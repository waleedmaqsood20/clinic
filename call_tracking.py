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

from . import crypto, repository


def _outcome(call: dict, booked: bool) -> str:
    if booked:
        return "booked"
    reason = (call.get("disconnection_reason") or "").lower()
    if "voicemail" in reason or "no_answer" in reason or "dial_no_answer" in reason:
        return "abandoned"
    return "info_given"


def persist_from_retell(session_factory, call: dict, analyzed: bool) -> None:
    call_id = call.get("call_id")
    number = call.get("from_number") or ""
    start, end = call.get("start_timestamp"), call.get("end_timestamp")
    duration = int((end - start) / 1000) if (start and end) else None
    analysis = call.get("call_analysis") or {}
    cost = (call.get("call_cost") or {}).get("combined_cost")

    with session_factory() as session:
        booked = repository.booking_exists_for_call(session, call_id)
        outcome = _outcome(call, booked)
        repository.upsert_call(
            session,
            call_id=call_id,
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
            ended_at=dt.datetime.now(dt.timezone.utc),
        )
        repository.write_audit(session, actor="voice_ai", action="call.recorded",
                               call_id=call_id, phi=True,
                               detail={"outcome": outcome, "analyzed": analyzed})
        session.commit()
