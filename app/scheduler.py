"""
Background schedulers: auto-sync from Retell, and day-before SMS reminders.

Both are best-effort while the process is awake. On Render's free tier the
service sleeps, so the reliable production path is an external cron hitting:
  POST /api/sync-retell     (auto-sync equivalent)
  POST /api/send-reminders  (reminder equivalent)
A paid Render plan (always awake) makes the in-process loops dependable.

Env:
  AUTO_SYNC_HOURS   every N hours pull /v2/list-calls + GHL appts (default 6; "0" disables)
  REMINDER_HOUR     local hour to send tomorrow's reminders (default 16; "" disables)
"""
from __future__ import annotations
import asyncio
import datetime as dt
import logging
import os
from zoneinfo import ZoneInfo

from . import call_tracking, repository, crypto, knowledge
from .tools import _fmt_day, _fmt_time

logger = logging.getLogger("clinic")


def _tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis"))


# ---------- day-before reminders ----------

def send_due_reminders(session_factory, sms_provider) -> dict:
    """Send an SMS for each confirmed appointment starting tomorrow (local).
    Idempotent: reminder_sent is flipped per appointment, so double runs
    (manual button + cron + scheduler) never double-text a patient."""
    tz = _tz()
    tomorrow = (dt.datetime.now(tz) + dt.timedelta(days=1)).date()
    sent = failed = 0
    with session_factory() as session:
        due = repository.appointments_needing_reminder(session, tomorrow)
        for appt in due:
            try:
                phone = crypto.decrypt(appt.caller_phone_enc)
            except Exception:
                phone = None
            if not phone:
                failed += 1
                continue
            start = appt.start_utc
            if start is not None and start.tzinfo is None:   # SQLite drops tz
                start = start.replace(tzinfo=dt.timezone.utc)
            local = start.astimezone(tz) if start else None
            when = (f"{_fmt_day(local.date())} at {_fmt_time(local)}"
                    if local else "tomorrow")
            body = (f"{knowledge.CLINIC_PROFILE['name']}: reminder — your "
                    f"{appt.service or 'appointment'} is {when}. "
                    f"Reply C to confirm, R to reschedule, X to cancel.")
            try:
                sms_provider.send(phone, body)
                appt.reminder_sent = True
                sent += 1
            except Exception:
                logger.exception("[REMIND] SMS failed for appointment %s", appt.id)
                failed += 1
        session.commit()
    logger.info("[REMIND] tomorrow=%s sent=%d failed=%d due=%d",
                tomorrow, sent, failed, len(due))
    return {"date": tomorrow.isoformat(), "due": len(due),
            "sent": sent, "failed": failed}


async def reminder_loop(session_factory, sms_provider) -> None:
    hour = int(os.getenv("REMINDER_HOUR", "16"))
    tz = _tz()
    logger.info("[REMIND] scheduler active — daily at %02d:00 %s", hour, tz)
    while True:
        now = dt.datetime.now(tz)
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await asyncio.to_thread(send_due_reminders, session_factory, sms_provider)
        except Exception:
            logger.exception("[REMIND] scheduled run failed")


# ---------- Retell auto-sync ----------

_ghl_first_run = True


async def _run_sync(session_factory, api_key: str, calendar) -> None:
    """One full sync pass: Retell calls → GHL appointments → no-show statuses."""
    global _ghl_first_run

    try:
        result = await asyncio.to_thread(
            call_tracking.sync_from_retell_api, session_factory, api_key)
        logger.info("[AUTOSYNC] retell: %s", result)
    except Exception:
        logger.exception("[AUTOSYNC] Retell sync failed")

    if calendar is not None and os.getenv("GHL_API_TOKEN"):
        try:
            # First run: full 120-day backfill to catch historical appointments.
            # Subsequent runs: only look back 2 hours — light on the GHL API
            # and fast enough for frequent polling (e.g. AUTO_SYNC_HOURS=0.083).
            days_back = 120 if _ghl_first_run else 0.083
            _ghl_first_run = False
            result = await asyncio.to_thread(
                call_tracking.sync_ghl_appointments, session_factory, calendar,
                days_back=days_back)
            logger.info("[AUTOSYNC] ghl-appts: %s", result)
        except Exception:
            logger.exception("[AUTOSYNC] GHL appointment sync failed")
        try:
            result = await asyncio.to_thread(
                call_tracking.sync_appointment_statuses, session_factory, calendar)
            logger.info("[AUTOSYNC] statuses: %s", result)
        except Exception:
            logger.exception("[AUTOSYNC] status sync failed")


async def auto_sync_loop(session_factory, calendar=None) -> None:
    hours = float(os.getenv("AUTO_SYNC_HOURS", "6"))
    api_key = os.getenv("RETELL_API_KEY")
    if hours <= 0 or not api_key:
        return
    logger.info("[AUTOSYNC] active — startup run in 60s, then every %.1fh", hours)
    # Run once shortly after startup so a Render restart / cold wake doesn't
    # leave data stale for up to 6h before the first scheduled pass.
    await asyncio.sleep(60)
    await _run_sync(session_factory, api_key, calendar)
    while True:
        await asyncio.sleep(hours * 3600)
        await _run_sync(session_factory, api_key, calendar)
