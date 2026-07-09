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
app.include_router(make_dashboard_router(SessionLocal))


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

    return JSONResponse(content={
        "call_inbound": {
            "dynamic_variables": {
                "week_availability": week_str,
                "current_date": today_str,
            }
        }
    })


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
    return JSONResponse(content={"received": True})
