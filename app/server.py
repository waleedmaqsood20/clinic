"""
The only server you run (Stage 1) — Retell + GoHighLevel.
Build: 2026-06-30-r4  (POST contacts/search, GET calendars/events, cancel via PUT)

  uvicorn app.server:app --port 4242   (run.sh does this for you)

Retell handles the phone call; this server exposes two endpoints:
  - POST /retell/function : the agent's custom-function calls (FAQ, availability,
    booking). We run the action and return one result string.
  - POST /retell/webhook  : call events (call_started / call_ended / call_analyzed).
    We record each call.

Both verify Retell's signature. With GHL keys it books into your GHL calendar and
creates the contact in your CRM; without them it uses a pretend calendar. With no
DATABASE_URL it stores data in a local SQLite file.
"""
from __future__ import annotations
import asyncio
import datetime as dt
import os
import json
import logging
from zoneinfo import ZoneInfo

logger = logging.getLogger("clinic")

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from .tools import ToolExecutor, handle_function_call, format_week_availability
from .providers import GHLCalendar, InMemoryCalendar, SmsProvider, TwilioSms
from . import db as dbmod, security, availability_cache
from .call_tracking import persist_from_retell
from .dashboard import make_dashboard_router

CLINIC_TZ = os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./clinic.db")

engine = dbmod.make_engine(DATABASE_URL)
SessionLocal = dbmod.make_session_factory(engine)
dbmod.init_db(engine)

app = FastAPI(title="Clinic Voice AI — Stage 1 (Retell + GHL)")
logger.info("=== Clinic Voice AI starting — build 2026-06-30-r4 ===")


def _make_calendar():
    token = os.getenv("GHL_API_TOKEN")
    location = os.getenv("GHL_LOCATION_ID")
    calendar = os.getenv("GHL_CALENDAR_ID")
    if token and location and calendar:
        return GHLCalendar(token=token, location_id=location, calendar_id=calendar,
                           timezone=CLINIC_TZ,
                           slot_minutes=int(os.getenv("GHL_SLOT_MINUTES", "30")))
    return InMemoryCalendar()          # pretend calendar for first tests


def _make_sms():
    if os.getenv("TWILIO_ACCOUNT_SID"):
        return TwilioSms(os.environ["TWILIO_ACCOUNT_SID"],
                         os.environ["TWILIO_AUTH_TOKEN"],
                         os.environ["TWILIO_FROM_NUMBER"])
    return SmsProvider()               # no-op; GHL can send its own confirmations


executor = ToolExecutor(_make_calendar(), _make_sms(), session_factory=SessionLocal)
app.include_router(make_dashboard_router(SessionLocal, sms_provider=executor.sms,
                                         calendar=executor.calendar))


@app.on_event("startup")
async def _start_schedulers():
    """Optional in-process schedulers. All best-effort on Render free tier
    (the service must be awake); external crons hitting POST /api/send-digest,
    /api/send-reminders and /api/sync-retell are the reliable triggers.

      DIGEST_HOUR      set → daily digest SMS
      REMINDER_HOUR    unset → defaults to 16; set "" to rely on cron only
      AUTO_SYNC_HOURS  default 6; "0" disables (needs RETELL_API_KEY)
    """
    from . import digest, scheduler
    if os.getenv("DIGEST_HOUR"):
        asyncio.create_task(digest.scheduler_loop(SessionLocal, executor.sms))
    if os.getenv("REMINDER_HOUR", "16") != "":
        asyncio.create_task(scheduler.reminder_loop(SessionLocal, executor.sms))
    asyncio.create_task(scheduler.auto_sync_loop(SessionLocal,
                                                 calendar=executor.calendar))
    # First admin user from env (no-op if users already exist)
    from . import auth as auth_mod
    try:
        auth_mod.bootstrap_admin(SessionLocal)
    except Exception:
        logger.exception("[AUTH] bootstrap failed")
    # Patient registry backfill — idempotent, links historical rows once.
    from . import repository as repo
    try:
        result = await asyncio.to_thread(repo.backfill_patients, SessionLocal)
        if any(result.values()):
            logger.info("[PATIENTS] backfill: %s", result)
    except Exception:
        logger.exception("[PATIENTS] backfill failed")


async def _prefetch_availability(call_id: str) -> None:
    """Fetch week availability in the background as soon as a call starts.

    Runs in a thread pool so the blocking GHL HTTP call doesn't touch the event
    loop. Result is stored in availability_cache keyed by call_id so the first
    get_week_availability tool call returns in <100ms from cache.
    """
    try:
        week = await asyncio.to_thread(executor.calendar.get_week_availability, "")
        availability_cache.put(call_id, week)
        logger.info("[PREFETCH] call_id=%s cached %d days", call_id, len(week))
    except Exception:
        logger.exception("[PREFETCH] failed for call_id=%s — tool will fall back to live fetch", call_id)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/health/deep")
async def health_deep():
    """DB + GHL + Retell connectivity check. Point an uptime pinger here —
    it verifies dependencies AND keeps the Render free tier awake."""
    checks: dict[str, dict] = {}

    # Database
    try:
        from sqlalchemy import text
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:
        checks["database"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # GHL (only when configured; InMemoryCalendar reports not_configured)
    if os.getenv("GHL_API_TOKEN"):
        try:
            week = await asyncio.wait_for(
                asyncio.to_thread(executor.calendar.get_week_availability, ""),
                timeout=8.0)
            checks["ghl"] = {"ok": True, "days_with_slots": len(week)}
        except Exception as exc:
            checks["ghl"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        checks["ghl"] = {"ok": True, "note": "not configured — in-memory calendar"}

    # Retell API key
    api_key = os.getenv("RETELL_API_KEY")
    if api_key:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post("https://api.retellai.com/v2/list-calls",
                                      headers={"Authorization": f"Bearer {api_key}"},
                                      json={"limit": 1})
            checks["retell"] = {"ok": r.status_code == 200,
                                **({} if r.status_code == 200
                                   else {"error": f"HTTP {r.status_code}"})}
        except Exception as exc:
            checks["retell"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        checks["retell"] = {"ok": True, "note": "not configured — dev mode"}

    all_ok = all(c.get("ok") for c in checks.values())
    return JSONResponse(status_code=200 if all_ok else 503,
                        content={"ok": all_ok, "checks": checks})


@app.get("/dev/test-inbound")
async def dev_test_inbound():
    """Dev-only: returns exactly what /retell/inbound would inject, no signature needed."""
    today = dt.datetime.now(ZoneInfo(CLINIC_TZ)).date()
    today_str = f"{today.strftime('%A, %B')} {today.day}, {today.year}"
    try:
        week = await asyncio.to_thread(executor.calendar.get_week_availability, "")
        week_str = format_week_availability(week)
        days = len(week)
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)
    return JSONResponse(content={
        "days_with_slots": days,
        "current_date_injected": today_str,
        "week_availability_injected": week_str,
        "retell_payload_sent": {
            "call_inbound": {
                "dynamic_variables": {
                    "week_availability": week_str,
                    "current_date": today_str,
                }
            }
        }
    })


@app.post("/retell/inbound")
async def retell_inbound(request: Request):
    """Retell inbound call webhook — fires while the phone is still ringing.

    We fetch week availability here and return it as {{week_availability}} so
    the LLM has the schedule in its prompt before the first turn. The begin_message
    fires immediately after (static audio, zero latency). No LLM compliance needed.
    """
    raw = await request.body()
    security.verify_retell_request(raw, request.headers.get("x-retell-signature"))
    body = json.loads(raw or b"{}")
    call_info = body.get("call_inbound", {})
    from_number = call_info.get("from_number", "?")

    today = dt.datetime.now(ZoneInfo(CLINIC_TZ)).date()
    today_str = f"{today.strftime('%A, %B')} {today.day}, {today.year}"

    try:
        week = await asyncio.wait_for(
            asyncio.to_thread(executor.calendar.get_week_availability, ""),
            timeout=6.0,
        )
        week_str = format_week_availability(week)
        logger.info("[INBOUND] from=%s week_days=%d today=%s", from_number, len(week), today_str)
    except asyncio.TimeoutError:
        logger.warning("[INBOUND] GHL fetch timed out after 6s — returning current_date only")
        week_str = ""
    except Exception:
        logger.exception("[INBOUND] availability fetch failed — injecting empty variable")
        week_str = ""

    # Returning-patient recognition: look the caller up in our registry and
    # give the agent their name + next appointment BEFORE the call connects.
    caller_context = ""
    try:
        caller_context = await asyncio.to_thread(
            _build_caller_context, from_number)
        if caller_context:
            logger.info("[INBOUND] recognized caller from=%s", from_number)
    except Exception:
        logger.exception("[INBOUND] caller lookup failed — proceeding anonymous")

    return JSONResponse(content={
        "call_inbound": {
            "dynamic_variables": {
                "week_availability": week_str,
                "current_date": today_str,
                "caller_context": caller_context,
                # inbound calls: outbound section of the prompt stays dormant
                "outbound_purpose": "",
                "outbound_context": "",
            }
        }
    })


def _fmt_local(start: dt.datetime) -> str:
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    local = start.astimezone(ZoneInfo(CLINIC_TZ))
    hour = local.hour % 12 or 12
    ap = "am" if local.hour < 12 else "pm"
    t = f"{hour}:{local.minute:02d}{ap}" if local.minute else f"{hour}{ap}"
    return f"{local.strftime('%A, %B')} {local.day} at {t}"


def _build_caller_context(from_number: str) -> str:
    """Rich but respectful context: name, relationship depth (visit count,
    last visit), and up to two upcoming appointments. Empty for unknowns."""
    from . import crypto, repository
    from .models import Appointment
    if not from_number or from_number == "?":
        return ""
    with SessionLocal() as session:
        patient = repository.find_patient_by_phone(session, from_number)
        if patient is None:
            return ""
        try:
            name = crypto.decrypt(patient.name_enc)
        except Exception:
            name = None
        parts = [f"Returning patient{f': {name}' if name else ' (name not on file)'}."]

        now = dt.datetime.now(dt.timezone.utc)
        appts = (session.query(Appointment)
                 .filter(Appointment.phone_hash == patient.phone_hash).all())

        def _aw(d):
            return d.replace(tzinfo=dt.timezone.utc) if (d and d.tzinfo is None) else d
        past = [a for a in appts
                if _aw(a.start_utc) and _aw(a.start_utc) < now
                and a.status not in ("cancelled", "invalid", "noshow", "no_show")]
        if past:
            last = max(past, key=lambda a: _aw(a.start_utc))
            visits = len(past)
            parts.append(f"{visits} previous visit{'s' if visits != 1 else ''}; "
                         f"last was {_fmt_local(last.start_utc).rsplit(' at ', 1)[0]}"
                         f" ({last.service or 'appointment'}).")
        upcoming = sorted((a for a in appts
                           if _aw(a.start_utc) and _aw(a.start_utc) >= now
                           and a.status == "confirmed"),
                          key=lambda a: _aw(a.start_utc))[:2]
        for a in upcoming:
            parts.append(f"Upcoming: {a.service or 'appointment'} on "
                         f"{_fmt_local(a.start_utc)}.")
        try:
            insurance = crypto.decrypt(patient.insurance_enc)
        except Exception:
            insurance = None
        if insurance:
            parts.append(f"Insurance on file: {insurance}.")
        return " ".join(parts)


@app.post("/retell/function")
async def retell_function(request: Request):
    raw = await request.body()
    security.verify_retell_request(raw, request.headers.get("x-retell-signature"))
    body = json.loads(raw or b"{}")
    fn_name = body.get("name", "?")
    has_args = body.get("args") is not None
    logger.info("[RETELL] fn=%s has_args=%s keys=%s", fn_name, has_args, list(body.keys()))
    result = handle_function_call(body, executor)
    logger.info("[RETELL] fn=%s -> %s", fn_name, result[:80])
    return JSONResponse(content={"result": result})


@app.post("/sms/inbound")
async def sms_inbound(request: Request):
    """Two-way SMS (Twilio webhook). Patients reply to reminders:
      C / CONFIRM     → confirmed (audited)
      X / CANCEL      → cancel next appointment in GHL + locally, offer freed
                        slot to the waitlist
      R / RESCHEDULE  → flag for follow-up; outbound call if configured
    Returns TwiML so Twilio texts our reply back."""
    form = await request.form()
    params = dict(form)

    # Twilio signature validation (skipped when no auth token configured)
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if auth_token and os.getenv("SMS_VALIDATE", "1") != "0":
        try:
            from twilio.request_validator import RequestValidator
            url = os.getenv("SMS_WEBHOOK_URL") or str(request.url)
            sig = request.headers.get("X-Twilio-Signature", "")
            if not RequestValidator(auth_token).validate(url, params, sig):
                return JSONResponse(status_code=403,
                                    content={"error": "bad twilio signature"})
        except ImportError:
            logger.warning("[SMS] twilio lib missing — signature not validated")

    from_number = params.get("From", "")
    body = (params.get("Body") or "").strip().upper()
    logger.info("[SMS] inbound from=%s body=%r", from_number, body[:40])

    def _twiml(msg: str):
        from fastapi.responses import Response as RawResponse
        safe = (msg.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
        return RawResponse(
            content=f'<?xml version="1.0" encoding="UTF-8"?>'
                    f"<Response><Message>{safe}</Message></Response>",
            media_type="application/xml")

    from . import repository
    from .knowledge import CLINIC_PROFILE

    word = body.split()[0] if body else ""
    if word in ("C", "CONFIRM", "YES", "Y"):
        with SessionLocal() as session:
            appt = repository.next_appointment_for_phone(session, from_number)
            repository.write_audit(session, actor="patient_sms",
                                   action="appointment.confirmed_by_sms",
                                   phi=True,
                                   detail={"appointment_id": appt.id if appt else None})
            session.commit()
        return _twiml(f"Thanks — you're confirmed. See you soon! "
                      f"— {CLINIC_PROFILE['name']}")

    if word in ("X", "CANCEL", "N", "NO"):
        with SessionLocal() as session:
            appt = repository.next_appointment_for_phone(session, from_number)
            ghl_id = appt.calcom_booking_uid if appt else None
        if not appt:
            return _twiml("We couldn't find an upcoming appointment for this "
                          f"number — give us a call at {CLINIC_PROFILE['phone']}.")
        try:
            if ghl_id:
                await asyncio.to_thread(executor.calendar.cancel, ghl_id)
        except Exception:
            logger.exception("[SMS] GHL cancel failed for %s", ghl_id)
            return _twiml("Sorry — we couldn't cancel automatically. Please "
                          f"call us at {CLINIC_PROFILE['phone']}.")
        freed = executor._sync_local_status(ghl_id, "cancelled")
        await asyncio.to_thread(executor._offer_freed_slot, freed)
        with SessionLocal() as session:
            repository.write_audit(session, actor="patient_sms",
                                   action="appointment.cancelled_by_sms",
                                   phi=True, detail={"ghl_id": ghl_id})
            session.commit()
        return _twiml("Done — your appointment is cancelled. Text or call "
                      "anytime to rebook. We hope to see you again soon!")

    if word in ("R", "RESCHEDULE"):
        with SessionLocal() as session:
            appt = repository.next_appointment_for_phone(session, from_number)
            if appt:
                appt.status = "reschedule_requested"
            repository.write_audit(session, actor="patient_sms",
                                   action="appointment.reschedule_requested",
                                   phi=True,
                                   detail={"appointment_id": appt.id if appt else None})
            session.commit()
        from . import outbound
        if os.getenv("OUTBOUND_ON_RESCHEDULE", "0") == "1" and outbound.configured():
            asyncio.get_event_loop().run_in_executor(
                None, outbound.create_call, from_number, "callback",
                "The patient texted asking to reschedule their upcoming "
                "appointment. Help them pick a new time.")
            return _twiml("No problem — Sarah is calling you right now to "
                          "find a new time.")
        return _twiml(f"No problem — we'll call you shortly to find a new "
                      f"time, or call us at {CLINIC_PROFILE['phone']}.")

    return _twiml(f"Reply C to confirm, R to reschedule, or X to cancel your "
                  f"appointment. — {CLINIC_PROFILE['name']}")


@app.post("/ghl/webhook")
async def ghl_webhook(request: Request, background: BackgroundTasks):
    """Real-time GHL appointment sync.

    Configure in GHL → Settings → Webhooks → URL: https://clinic-xprt.onrender.com/ghl/webhook
    Events to subscribe: AppointmentCreate, AppointmentUpdate, AppointmentDelete

    Optional: set GHL_WEBHOOK_SECRET in env to enable HMAC-SHA256 signature validation.
    """
    raw = await request.body()

    secret = os.getenv("GHL_WEBHOOK_SECRET")
    if secret:
        import hmac, hashlib
        sig = request.headers.get("x-wc-webhook-signature", "")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning("[GHL-WEBHOOK] bad signature — rejected")
            return JSONResponse(status_code=401, content={"error": "bad signature"})

    body = json.loads(raw or b"{}")
    event_type = body.get("type", "")
    logger.info("[GHL-WEBHOOK] type=%s id=%s", event_type, body.get("id", "?"))

    if event_type not in ("AppointmentCreate", "AppointmentUpdate", "AppointmentDelete"):
        return JSONResponse(content={"received": True, "action": "ignored"})

    if not isinstance(executor.calendar, GHLCalendar):
        return JSONResponse(content={"received": True, "action": "ghl_not_configured"})

    from .call_tracking import handle_ghl_appointment_event
    background.add_task(handle_ghl_appointment_event, SessionLocal,
                        executor.calendar, body)
    return JSONResponse(content={"received": True})


@app.post("/retell/webhook")
async def retell_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    security.verify_retell_request(raw, request.headers.get("x-retell-signature"))
    body = json.loads(raw or b"{}")
    event = body.get("event")
    call = body.get("call") or {}
    logger.info("[WEBHOOK] event=%s call_id=%s", event, call.get("call_id", "?"))
    if event == "call_started":
        call_id = call.get("call_id", "")
        if call_id:
            background.add_task(_prefetch_availability, call_id)
            logger.info("[WEBHOOK] call_started call_id=%s — prefetch queued", call_id)
    elif event in ("call_ended", "call_analyzed"):
        background.add_task(persist_from_retell, SessionLocal, call,
                            event == "call_analyzed")
        if event == "call_analyzed" and call.get("call_id"):
            # Runs after persist (FastAPI executes background tasks in order):
            # cross-check that a 'booked' call really has a live GHL appointment.
            from .call_tracking import verify_booking
            background.add_task(verify_booking, SessionLocal,
                                executor.calendar, call["call_id"])
    return JSONResponse(content={"received": True})
