"""
Outbound calls via Retell (POST /v2/create-phone-call).

Uses the SAME agent as inbound — the prompt's Outbound section activates when
{{outbound_purpose}} is non-empty, so Sarah knows she placed the call and why.

Requires: RETELL_API_KEY + RETELL_PHONE_NUMBER (the clinic's Retell number).

Uses:
  - staff "Call patient" button on the profile (purpose: callback)
  - confirm tomorrow's appointments by voice (purpose: confirm_appointment)
  - offer a freed slot to a waitlisted caller (purpose: waitlist_offer)
"""
from __future__ import annotations
import logging
import os

logger = logging.getLogger("clinic")

RETELL_API = "https://api.retellai.com"


def configured() -> bool:
    return bool(os.getenv("RETELL_API_KEY") and os.getenv("RETELL_PHONE_NUMBER"))


def create_call(to_number: str, purpose: str, context: str = "",
                client=None) -> dict:
    """Place one outbound call. Returns {'ok', 'call_id'|'error'}."""
    if not configured():
        return {"ok": False,
                "error": "outbound not configured — set RETELL_API_KEY and RETELL_PHONE_NUMBER"}
    if not to_number or not to_number.startswith("+"):
        return {"ok": False, "error": f"bad destination number {to_number!r}"}
    if client is None:
        import httpx
        client = httpx.Client(timeout=15.0)
    try:
        r = client.post(
            f"{RETELL_API}/v2/create-phone-call",
            headers={"Authorization": f"Bearer {os.environ['RETELL_API_KEY']}",
                     "Content-Type": "application/json"},
            json={
                "from_number": os.environ["RETELL_PHONE_NUMBER"],
                "to_number": to_number,
                "retell_llm_dynamic_variables": {
                    "outbound_purpose": purpose,
                    "outbound_context": context,
                    "caller_context": "",
                    "week_availability": "",
                    "current_date": "",
                },
            })
        if r.status_code not in (200, 201):
            logger.error("[OUTBOUND] create-call failed %s: %s",
                         r.status_code, r.text[:200])
            return {"ok": False, "error": f"Retell HTTP {r.status_code}"}
        d = r.json() or {}
        call_id = d.get("call_id")
        logger.info("[OUTBOUND] purpose=%s to=%s call_id=%s",
                    purpose, to_number, call_id)
        return {"ok": True, "call_id": call_id}
    except Exception as exc:
        logger.exception("[OUTBOUND] create-call error")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def confirm_tomorrows_appointments(session_factory, client=None) -> dict:
    """Place a confirmation call for each of tomorrow's confirmed appointments
    (voice alternative/addition to the SMS reminder)."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    from . import crypto, repository

    tz = ZoneInfo(os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis"))
    tomorrow = (dt.datetime.now(tz) + dt.timedelta(days=1)).date()
    placed = failed = 0
    with session_factory() as session:
        due = repository.appointments_needing_reminder(session, tomorrow)
        jobs = []
        for a in due:
            try:
                phone = crypto.decrypt(a.caller_phone_enc)
                name = crypto.decrypt(a.caller_name_enc) or "the patient"
            except Exception:
                failed += 1
                continue
            start = a.start_utc
            if start is not None and start.tzinfo is None:
                start = start.replace(tzinfo=dt.timezone.utc)
            local = start.astimezone(tz) if start else None
            if local:
                h = local.hour % 12 or 12
                ap = "am" if local.hour < 12 else "pm"
                t = f"{h}:{local.minute:02d}{ap}" if local.minute else f"{h}{ap}"
                when = f"{local.strftime('%A')} at {t}"
            else:
                when = "tomorrow"
            jobs.append((phone, name, a.service or "appointment", when))
    for phone, name, service, when in jobs:
        res = create_call(
            phone, purpose="confirm_appointment",
            context=f"Confirming {name}'s {service} {when}. "
                    f"If they can't make it, offer to reschedule or cancel.",
            client=client)
        placed += 1 if res.get("ok") else 0
        failed += 0 if res.get("ok") else 1
    return {"date": tomorrow.isoformat(), "due": len(jobs) + failed,
            "placed": placed, "failed": failed}


def offer_slot_to_waitlist(phone: str, name: str | None, service: str,
                           slot_detail: str, client=None) -> dict:
    """Call a waitlisted patient about a freed slot."""
    return create_call(
        phone, purpose="waitlist_offer",
        context=(f"{name or 'The caller'} asked to be waitlisted for "
                 f"{service or 'an appointment'}. A slot just opened: {slot_detail}. "
                 f"Offer it; if they accept, book it with book_appointment."),
        client=client)
