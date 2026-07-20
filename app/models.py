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


class Clinic(Base):
    """Multi-clinic foundation. Every data row carries a clinic_id.

    Single-clinic deployments get one row auto-created at startup from env
    (CLINIC_NAME / CLINIC_TZ). Onboarding clinic #2 = insert a row + per-number
    routing (see CLAUDE.md 'Multi-clinic' section for the remaining work).
    """
    __tablename__ = "clinics"

    id          = Column(Integer, primary_key=True)
    name        = Column(String, unique=True)
    timezone    = Column(String, default="America/Indiana/Indianapolis")
    phone       = Column(String, nullable=True)   # the clinic's inbound number
    active      = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), default=_now_utc)


class Patient(Base):
    """Patient registry — one row per person, matched by phone fingerprint.

    Calls and appointments link here via patient_id. Names/insurance are
    updated on each booking so the registry converges on the best-known data.
    """
    __tablename__ = "patients"

    id            = Column(Integer, primary_key=True)
    clinic_id     = Column(Integer, index=True, default=1)
    phone_hash    = Column(String, index=True, unique=True)
    phone_enc     = Column(LargeBinary)
    name_enc      = Column(LargeBinary)
    dob_enc       = Column(LargeBinary, nullable=True)     # reserved: intake
    insurance_enc = Column(LargeBinary, nullable=True)
    notes_enc     = Column(LargeBinary, nullable=True)     # staff notes
    first_seen_at = Column(DateTime(timezone=True), default=_now_utc)
    created_at    = Column(DateTime(timezone=True), default=_now_utc)


class DashboardUser(Base):
    """Per-user dashboard logins (replaces the single shared token; the token
    still works as a legacy fallback so existing links don't break)."""
    __tablename__ = "dashboard_users"

    id                  = Column(Integer, primary_key=True)
    clinic_id           = Column(Integer, index=True, default=1)
    username            = Column(String, unique=True, index=True)
    password_hash       = Column(String)                 # pbkdf2: "salt$hexdigest"
    role                = Column(String, default="staff")   # "admin" | "staff"
    active              = Column(Boolean, default=True)
    created_at          = Column(DateTime(timezone=True), default=_now_utc)
    last_login_at       = Column(DateTime(timezone=True), nullable=True)
    password_changed_at = Column(DateTime(timezone=True), nullable=True)


class LoginAttempt(Base):
    """Persistent login-failure log for brute-force throttling.
    Replaces the in-memory dict which reset on every Render restart."""
    __tablename__ = "login_attempts"

    id        = Column(Integer, primary_key=True)
    key       = Column(String, index=True)       # client IP
    failed_at = Column(DateTime(timezone=True), default=_now_utc)


class RevokedToken(Base):
    """JWT jti blacklist — enables true logout and post-password-change invalidation."""
    __tablename__ = "revoked_tokens"

    jti        = Column(String, primary_key=True)   # UUID from JWT jti claim
    revoked_at = Column(DateTime(timezone=True), default=_now_utc)


class WaitlistEntry(Base):
    """Callers waiting for a slot. Filled automatically when a matching
    appointment is cancelled, or manually from the dashboard."""
    __tablename__ = "waitlist"

    id             = Column(Integer, primary_key=True)
    clinic_id      = Column(Integer, index=True, default=1)
    patient_id     = Column(Integer, nullable=True, index=True)
    call_id        = Column(String, nullable=True)
    phone_hash     = Column(String, index=True)
    phone_enc      = Column(LargeBinary)
    name_enc       = Column(LargeBinary)
    service        = Column(String)
    preferred_day  = Column(String, nullable=True)   # YYYY-MM-DD or None=any
    time_note      = Column(String, nullable=True)   # "mornings", "after 3pm"
    status         = Column(String, default="waiting")  # waiting|offered|booked|removed
    offered_at     = Column(DateTime(timezone=True), nullable=True)
    offered_detail = Column(String, nullable=True)   # what slot was offered
    created_at     = Column(DateTime(timezone=True), default=_now_utc)


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
    insurance_enc     = Column(LargeBinary)      # encrypted insurance info (intake)
    phone_hash        = Column(String, index=True)
    patient_id        = Column(Integer, nullable=True, index=True)  # → patients.id
    clinic_id         = Column(Integer, index=True, default=1)
    status            = Column(String, default="confirmed")
    reminder_sent     = Column(Boolean, default=False)  # day-before SMS reminder
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
    booking_verified = Column(Boolean, nullable=True)   # None=n/a or unchecked
    patient_id       = Column(Integer, nullable=True, index=True)  # → patients.id
    clinic_id        = Column(Integer, index=True, default=1)
    ended_at         = Column(DateTime(timezone=True))
    created_at       = Column(DateTime(timezone=True), default=_now_utc)


class FailedEvent(Base):
    """Dead-letter queue: webhook payloads that failed to persist.

    persist_from_retell used to only log exceptions — a transient DB error
    meant the call was silently lost. Now the raw payload is stored here so
    it can be replayed from the dashboard.
    """
    __tablename__ = "failed_events"

    id          = Column(Integer, primary_key=True)
    source      = Column(String, default="retell_webhook")
    call_id     = Column(String, nullable=True, index=True)
    payload     = Column(JSON)                   # raw call dict from Retell
    error       = Column(String)                 # exception summary
    replayed    = Column(Boolean, default=False)
    created_at  = Column(DateTime(timezone=True), default=_now_utc)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id          = Column(Integer, primary_key=True)
    occurred_at = Column(DateTime(timezone=True), default=_now_utc)
    actor       = Column(String)
    action      = Column(String, index=True)
    call_id     = Column(String, nullable=True)
    phi         = Column(Boolean, default=False)
    detail      = Column(JSON, nullable=True)
