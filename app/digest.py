"""
Daily digest — yesterday's call stats for the clinic, sent by SMS.

Two ways to trigger:
  1. POST /api/send-digest (dashboard button, or an external cron/uptime
     pinger — the reliable option on Render's free tier, which sleeps).
  2. Optional in-process scheduler: set DIGEST_HOUR (0-23, clinic local time)
     and DIGEST_TO_NUMBER in the environment. Only fires while the service
     is awake, so treat it as best-effort until Render is on a paid plan.

The digest contains counts only — no names, numbers, or transcript content —
so sending it over SMS does not expose PHI.
"""
from __future__ import annotations
import asyncio
import datetime as dt
import logging
import os
from zoneinfo import ZoneInfo

from . import repository

logger = logging.getLogger("clinic")


def build_digest_text(session, day_local: dt.date | None = None) -> str:
    s = repository.digest_stats(session, day_local)
    rate = round(s["booked"] / s["total_calls"] * 100) if s["total_calls"] else 0
    mins, secs = divmod(s["avg_duration_seconds"], 60)
    return (f"Bright Smile daily digest — {s['date']}\n"
            f"Calls: {s['total_calls']} | Booked: {s['booked']} ({rate}%)\n"
            f"New patients: {s.get('new_patients', 0)}\n"
            f"Info given: {s['info_given']} | Abandoned: {s['abandoned']}\n"
            f"Avg duration: {mins}m {secs}s")


def send_digest(session_factory, sms_provider,
                day_local: dt.date | None = None) -> dict:
    to_number = os.getenv("DIGEST_TO_NUMBER")
    with session_factory() as session:
        text = build_digest_text(session, day_local)
    if not to_number:
        logger.info("[DIGEST] DIGEST_TO_NUMBER not set — digest built but not sent")
        return {"sent": False, "reason": "DIGEST_TO_NUMBER not set", "text": text}
    try:
        sms_provider.send(to_number, text)
        logger.info("[DIGEST] sent to %s", to_number)
        return {"sent": True, "to": to_number, "text": text}
    except Exception as exc:
        logger.exception("[DIGEST] send failed")
        return {"sent": False, "reason": f"{type(exc).__name__}: {exc}", "text": text}


async def scheduler_loop(session_factory, sms_provider) -> None:
    """Best-effort daily scheduler. Started from server startup only when
    DIGEST_HOUR is set. Sleeps until the next occurrence of DIGEST_HOUR in
    clinic local time, sends, repeats."""
    tz = ZoneInfo(os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis"))
    hour = int(os.getenv("DIGEST_HOUR", "7"))
    logger.info("[DIGEST] scheduler active — daily at %02d:00 %s", hour, tz)
    while True:
        now = dt.datetime.now(tz)
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await asyncio.to_thread(send_digest, session_factory, sms_provider)
        except Exception:
            logger.exception("[DIGEST] scheduled send failed")
