"""
SQLAlchemy ORM models: Appointment, Call, AuditLog.

Sensitive columns end in _enc (AES-256-GCM encrypted bytes).
phone_hash is a one-way HMAC fingerprint — used for matching without
storing the number in plain text.
"""
from __future__ import annotations
import datetime as dt

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, JSON, LargeBinary, String,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _now_utc():
    return dt.datetime.now(dt.timezone.utc)


class Appointment(Base):
    __tablename__ = "appointments"

    id                = Column(Integer, primary_key=True)
    call_id           = Column(String, index=True)
    service           = Column(String)
    start_utc         = Column(DateTime(timezone=True))
    calcom_booking_uid = Column(String)          # holds the GHL appointment id
    caller_name_enc   = Column(LargeBinary)      # encrypted patient name
    caller_phone_enc  = Column(LargeBinary)      # encrypted phone number
    reason_enc        = Column(LargeBinary)      # encrypted reason for visit
    phone_hash        = Column(String, index=True)
    patient_id        = Column(String, nullable=True)   # reserved for Stage 2
    status            = Column(String, default="confirmed")
    created_at        = Column(DateTime(timezone=True), default=_now_utc)


class Call(Base):
    __tablename__ = "calls"

    id               = Column(Integer, primary_key=True)
    call_id          = Column(String, unique=True, index=True)
    phone_hash       = Column(String, index=True)
    phone_enc        = Column(LargeBinary)
    duration_seconds = Column(Integer)
    intent           = Column(String)            # "booking" | "enquiry"
    outcome          = Column(String)            # "booked" | "info_given" | "abandoned"
    booked           = Column(Boolean, default=False)
    ended_reason     = Column(String)
    summary_enc      = Column(LargeBinary)
    transcript_enc   = Column(LargeBinary)
    recording_ref    = Column(String)
    cost_usd         = Column(Float)
    ended_at         = Column(DateTime(timezone=True))
    created_at       = Column(DateTime(timezone=True), default=_now_utc)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id          = Column(Integer, primary_key=True)
    occurred_at = Column(DateTime(timezone=True), default=_now_utc)
    actor       = Column(String)
    action      = Column(String, index=True)
    call_id     = Column(String, nullable=True)
    phi         = Column(Boolean, default=False)
    detail      = Column(JSON, nullable=True)
