"""
The filing clerk — the only place that reads/writes database rows.

Sensitive fields are encrypted on the way in via app.crypto.
"""
from __future__ import annotations
import datetime as dt

from . import crypto
from .models import Appointment, AuditLog, Call


def record_booking(session, *, call_id: str | None, caller_phone: str,
                   name: str, service: str, start_utc: dt.datetime,
                   confirmation: str, reason: str | None = None) -> Appointment:
    appt = Appointment(
        call_id=call_id,
        service=service,
        start_utc=start_utc,
        calcom_booking_uid=confirmation,
        caller_name_enc=crypto.encrypt(name),
        caller_phone_enc=crypto.encrypt(caller_phone),
        reason_enc=crypto.encrypt(reason),
        phone_hash=crypto.phone_hash(caller_phone),
    )
    session.add(appt)
    return appt


def write_audit(session, *, actor: str, action: str, call_id: str | None = None,
                phi: bool = False, detail: dict | None = None) -> AuditLog:
    log = AuditLog(actor=actor, action=action, call_id=call_id,
                   phi=phi, detail=detail)
    session.add(log)
    return log


def booking_exists_for_call(session, call_id: str | None) -> bool:
    if not call_id:
        return False
    return session.query(Appointment).filter_by(call_id=call_id).first() is not None


def upsert_call(session, *, call_id: str, **fields) -> Call:
    call = session.query(Call).filter_by(call_id=call_id).one_or_none()
    if call is None:
        call = Call(call_id=call_id)
        session.add(call)
    for k, v in fields.items():
        setattr(call, k, v)
    return call


def list_recent_calls(session, limit: int = 100) -> list[Call]:
    return session.query(Call).order_by(Call.id.desc()).limit(limit).all()


def get_kpis(session) -> dict:
    from sqlalchemy import func
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - dt.timedelta(days=today_start.weekday())

    total = session.query(func.count(Call.id)).scalar() or 0
    calls_today = (session.query(func.count(Call.id))
                   .filter(Call.ended_at >= today_start).scalar() or 0)
    calls_this_week = (session.query(func.count(Call.id))
                       .filter(Call.ended_at >= week_start).scalar() or 0)
    booked_total = (session.query(func.count(Call.id))
                    .filter(Call.booked == True).scalar() or 0)
    avg_dur = (session.query(func.avg(Call.duration_seconds))
               .filter(Call.duration_seconds.isnot(None)).scalar() or 0)
    outcome_rows = (session.query(Call.outcome, func.count(Call.id))
                    .filter(Call.outcome.isnot(None))
                    .group_by(Call.outcome).all())
    return {
        "calls_today": calls_today,
        "calls_this_week": calls_this_week,
        "total_calls": total,
        "booked_total": booked_total,
        "booking_rate_pct": round(booked_total / total * 100, 1) if total else 0,
        "avg_duration_seconds": int(avg_dur),
        "outcome_breakdown": {o: c for o, c in outcome_rows},
    }
