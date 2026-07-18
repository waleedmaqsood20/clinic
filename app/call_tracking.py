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
    except Exception as exc:
        logger.exception("[RETELL] persist_from_retell failed for call %s", call.get("call_id"))
        _dead_letter(session_factory, call, exc)


def _dead_letter(session_factory, call: dict, exc: Exception) -> None:
    """Store the raw payload so a failed webhook can be replayed later."""
    try:
        with session_factory() as session:
            repository.record_failed_event(
                session, source="retell_webhook", call_id=call.get("call_id"),
                payload=call, error=f"{type(exc).__name__}: {exc}")
            session.commit()
    except Exception:
        logger.exception("[DEAD-LETTER] could not store failed event %s",
                         call.get("call_id"))


def verify_booking(session_factory, calendar, call_id: str) -> None:
    """Post-call verification: a call marked 'booked' must have a matching,
    non-cancelled appointment in GHL. Catches silent booking failures.

    Sets Call.booking_verified: True (found + active), False (missing or
    cancelled in GHL), None (couldn't check — GHL error, no change).
    """
    from .models import Appointment, Call

    with session_factory() as session:
        db_call = session.query(Call).filter_by(call_id=call_id).one_or_none()
        if db_call is None or not db_call.booked:
            return
        appt = (session.query(Appointment)
                .filter_by(call_id=call_id)
                .order_by(Appointment.id.desc()).first())
        ghl_id = appt.calcom_booking_uid if appt else None

    if not ghl_id:
        verified = False        # we recorded 'booked' but have no appointment row
    else:
        try:
            event = calendar.get_appointment(ghl_id)
        except Exception:
            logger.exception("[VERIFY] GHL lookup failed for %s — leaving unverified",
                             call_id)
            return
        status = (event or {}).get("appointmentStatus", "")
        verified = bool(event) and status not in ("cancelled", "invalid", "noshow")

    with session_factory() as session:
        db_call = session.query(Call).filter_by(call_id=call_id).one_or_none()
        if db_call is None:
            return
        db_call.booking_verified = verified
        if not verified:
            repository.write_audit(session, actor="system",
                                   action="booking.verification_failed",
                                   call_id=call_id, phi=False,
                                   detail={"ghl_appointment_id": ghl_id})
        session.commit()
    logger.info("[VERIFY] call=%s booking_verified=%s", call_id, verified)


def sync_appointment_statuses(session_factory, calendar) -> dict:
    """No-show tracking: pull final statuses from GHL for past appointments
    still marked 'confirmed' in our DB (completed / noshow / cancelled)."""
    import datetime as dt
    from .models import Appointment
    now = dt.datetime.now(dt.timezone.utc)
    updated = checked = errors = 0
    with session_factory() as session:
        past = (session.query(Appointment)
                .filter(Appointment.start_utc < now)
                .filter(Appointment.status == "confirmed")
                .filter(Appointment.calcom_booking_uid.isnot(None))
                .limit(200).all())
        for a in past:
            checked += 1
            try:
                event = calendar.get_appointment(a.calcom_booking_uid)
            except Exception:
                logger.exception("[STATUS-SYNC] GHL lookup failed for appt %s", a.id)
                errors += 1
                continue
            if event is None:
                a.status = "cancelled"          # gone from GHL
                updated += 1
                continue
            status = (event.get("appointmentStatus") or "").lower()
            if status and status != "confirmed":
                a.status = "noshow" if status in ("noshow", "no_show") else status
                updated += 1
        session.commit()
    logger.info("[STATUS-SYNC] checked=%d updated=%d errors=%d",
                checked, updated, errors)
    return {"checked": checked, "updated": updated, "errors": errors}


def replay_failed_event(session_factory, event_id: int) -> dict:
    """Re-run _do_persist for a dead-lettered payload; mark replayed on success."""
    from .models import FailedEvent
    with session_factory() as session:
        ev = session.get(FailedEvent, event_id)
        if ev is None:
            return {"ok": False, "error": "event not found"}
        payload = dict(ev.payload or {})
    try:
        _do_persist(session_factory, payload, analyzed=True)
    except Exception as exc:
        logger.exception("[DEAD-LETTER] replay failed for event %s", event_id)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    with session_factory() as session:
        ev = session.get(FailedEvent, event_id)
        ev.replayed = True
        session.commit()
    return {"ok": True, "call_id": payload.get("call_id")}


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
        for call in calls:
            try:
                # normalise timestamps before passing to _do_persist
                for ts_field in ("start_timestamp", "end_timestamp"):
                    if call.get(ts_field) is not None:
                        call[ts_field] = _normalize_ms(call[ts_field])
                _do_persist(session_factory, call, analyzed=True)
                synced += 1
            except Exception:
                logger.exception("sync: failed to persist call %s", call.get("call_id"))
                skipped += 1
        # Stop only when Retell returns a short (final) page — a page whose
        # rows all failed to persist must not silently abort pagination.
        if len(calls) < 100:
            break
        pagination_key = calls[-1]["call_id"]

    client.close()
    return {"synced": synced, "skipped": skipped}


def sync_ghl_appointments(session_factory, calendar) -> dict:
    """Pull GHL appointments (past 120 days + next 90) into local DB.

    Fixes the silent-failure window when appointments.status was missing:
    GHL had the bookings, our DB didn't. After running this, Call.booked
    and Call.outcome are corrected and the KPI booking rate is accurate.
    """
    import datetime as dt
    from . import crypto, repository
    from .models import Appointment, Call

    if not hasattr(calendar, "fetch_calendar_events_range"):
        return {"error": "GHL not configured"}

    try:
        events = calendar.fetch_calendar_events_range(days_back=120, days_ahead=90)
    except Exception as exc:
        logger.exception("[GHL-SYNC] fetch failed")
        return {"error": str(exc)}

    created = updated = linked = skipped = errors = 0

    for e in events:
        try:
            event_id = e.get("id")
            contact_id = e.get("contactId")
            status = (e.get("appointmentStatus") or "confirmed").lower()
            start_ms = e.get("startTime")
            service = e.get("title") or ""

            if not event_id or not start_ms:
                skipped += 1
                continue

            try:
                phone = calendar._get_contact_phone(contact_id) if contact_id else None
            except Exception:
                phone = None

            start_utc = dt.datetime.fromtimestamp(int(start_ms) / 1000,
                                                  tz=dt.timezone.utc)

            with session_factory() as session:
                existing = repository.find_appointment_by_ghl_id(session, event_id)
                if existing:
                    if existing.status != status:
                        existing.status = status
                        updated += 1
                        session.commit()
                    else:
                        skipped += 1
                    continue

                if not phone:
                    skipped += 1
                    continue

                ph = crypto.phone_hash(phone)

                window_start = start_utc - dt.timedelta(minutes=90)
                window_end = start_utc + dt.timedelta(minutes=15)
                matching_call = (
                    session.query(Call)
                    .filter(Call.phone_hash == ph)
                    .filter(Call.ended_at >= window_start)
                    .filter(Call.ended_at <= window_end)
                    .order_by(Call.ended_at.desc())
                    .first()
                )

                appt = Appointment(
                    call_id=matching_call.call_id if matching_call else None,
                    service=service,
                    start_utc=start_utc,
                    calcom_booking_uid=event_id,
                    caller_phone_enc=crypto.encrypt(phone),
                    caller_name_enc=None,
                    reason_enc=None,
                    phone_hash=ph,
                    status=status,
                )
                patient = repository.upsert_patient(session, phone=phone)
                if patient:
                    appt.patient_id = patient.id
                session.add(appt)

                if matching_call and not matching_call.booked:
                    matching_call.booked = True
                    matching_call.outcome = "booked"
                    matching_call.intent = "booking"
                    linked += 1

                session.commit()
                created += 1

        except Exception:
            logger.exception("[GHL-SYNC] error processing event %s", e.get("id"))
            errors += 1

    logger.info("[GHL-SYNC] events=%d created=%d updated=%d linked=%d skipped=%d errors=%d",
                len(events), created, updated, linked, skipped, errors)
    return {"events_from_ghl": len(events), "appointments_created": created,
            "statuses_updated": updated, "calls_linked": linked,
            "skipped": skipped, "errors": errors}


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
        patient = repository.upsert_patient(session, phone=number,
                                            first_seen_at=ended_at)
        upsert_fields = dict(
            patient_id=patient.id if patient else None,
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
