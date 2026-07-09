"""
Call tracking.

Persists Retell's call events into the calls table:
  - call_ended    -> we have transcript, duration, disconnection reason
  - call_analyzed -> we additionally have the summary + success/sentiment

We upsert by call_id so both events update the same row. Outcome is 'booked' if
an appointment was created during this call (recorded at booking time), else it's
derived from the disconnection reason.
"""
from __future__ import annotations
import datetime as dt
import logging

from sqlalchemy.exc import IntegrityError

from . import crypto, repository

logger = logging.getLogger("clinic")


def _outcome(call: dict, booked: bool) -> str:
    if booked:
        return "booked"
    reason = (call.get("disconnection_reason") or "").lower()
    if "voicemail" in reason or "no_answer" in reason or "dial_no_answer" in reason:
        return "abandoned"
    return "info_given"


def persist_from_retell(session_factory, call: dict, analyzed: bool) -> None:
    try:
        _do_persist(session_factory, call, analyzed)
    except Exception:
        logger.exception("[RETELL] persist_from_retell failed for call %s", call.get("call_id"))


def _normalize_ms(v) -> int | None:
    """Retell mixes epoch units across endpoints — normalise to milliseconds."""
    if v is None:
        return None
    n = int(v)
    abs_n = abs(n)
    if abs_n >= 10 ** 14:
        return n // 1000   # microseconds → ms
    if abs_n >= 10 ** 11:
        return n           # already ms
    return n * 1000        # seconds → ms


def sync_from_retell_api(session_factory, retell_api_key: str) -> dict:
    """Pull all calls from Retell /v2/list-calls and upsert into Postgres."""
    import httpx
    client = httpx.Client(timeout=30.0)
    headers = {"Authorization": f"Bearer {retell_api_key}",
               "Content-Type": "application/json"}
    synced = skipped = 0
    pagination_key = None

    while True:
        body: dict = {"limit": 100}
        if pagination_key:
            body["pagination_key"] = pagination_key
        r = client.post("https://api.retellai.com/v2/list-calls",
                        headers=headers, json=body)
        if r.status_code != 200:
            logger.error("Retell list-calls failed %s: %s", r.status_code, r.text[:200])
            break
        calls = r.json()
        if not calls:
            break
        new_count = 0
        for call in calls:
            try:
                # normalise timestamps before passing to _do_persist
                for ts_field in ("start_timestamp", "end_timestamp"):
                    if call.get(ts_field) is not None:
                        call[ts_field] = _normalize_ms(call[ts_field])
                _do_persist(session_factory, call, analyzed=True)
                new_count += 1
                synced += 1
            except Exception:
                logger.exception("sync: failed to persist call %s", call.get("call_id"))
                skipped += 1
        if not new_count:
            break
        pagination_key = calls[-1]["call_id"]

    client.close()
    return {"synced": synced, "skipped": skipped}


def _do_persist(session_factory, call: dict, analyzed: bool) -> None:
    call_id = call.get("call_id")
    number = call.get("from_number") or ""
    start, end = call.get("start_timestamp"), call.get("end_timestamp")
    duration = int((end - start) / 1000) if (start and end) else None
    ended_at = (dt.datetime.fromtimestamp(end / 1000, tz=dt.timezone.utc)
                if end else dt.datetime.now(dt.timezone.utc))
    analysis = call.get("call_analysis") or {}
    cost = (call.get("call_cost") or {}).get("combined_cost")

    with session_factory() as session:
        booked = repository.booking_exists_for_call(session, call_id)
        outcome = _outcome(call, booked)
        upsert_fields = dict(
            phone_hash=crypto.phone_hash(number),
            phone_enc=crypto.encrypt(number),
            ended_reason=call.get("disconnection_reason"),
            duration_seconds=duration,
            intent="booking" if booked else "enquiry",
            outcome=outcome,
            booked=booked,
            summary_enc=crypto.encrypt(analysis.get("call_summary")),
            transcript_enc=crypto.encrypt(call.get("transcript")),
            recording_ref=call.get("recording_url"),
            cost_usd=cost,
            ended_at=ended_at,
        )
        repository.upsert_call(session, call_id=call_id, **upsert_fields)
        repository.write_audit(session, actor="voice_ai", action="call.recorded",
                               call_id=call_id, phi=True,
                               detail={"outcome": outcome, "analyzed": analyzed})
        try:
            session.commit()
        except IntegrityError:
            # call_ended and call_analyzed fire simultaneously — the other task
            # already inserted this call_id. Roll back and retry as an update.
            session.rollback()
            repository.upsert_call(session, call_id=call_id, **upsert_fields)
            session.commit()
