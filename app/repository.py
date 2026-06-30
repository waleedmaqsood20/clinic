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
