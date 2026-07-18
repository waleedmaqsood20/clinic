"""
The filing clerk — the only place that reads/writes database rows.

Sensitive fields are encrypted on the way in via app.crypto.
"""
from __future__ import annotations
import datetime as dt

from . import crypto
from .models import (Appointment, AuditLog, Call, FailedEvent, Patient,
                     WaitlistEntry)


def _clinic_tz():
    import os
    from zoneinfo import ZoneInfo
    return ZoneInfo(os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis"))


def record_booking(session, *, call_id: str | None, caller_phone: str,
                   name: str, service: str, start_utc: dt.datetime,
                   confirmation: str, reason: str | None = None,
                   insurance: str | None = None) -> Appointment:
    appt = Appointment(
        call_id=call_id,
        service=service,
        start_utc=start_utc,
        calcom_booking_uid=confirmation,
        caller_name_enc=crypto.encrypt(name),
        caller_phone_enc=crypto.encrypt(caller_phone),
        reason_enc=crypto.encrypt(reason),
        insurance_enc=crypto.encrypt(insurance),
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


def list_recent_calls(session, limit: int = 100, offset: int = 0,
                      outcome: str | None = None,
                      date_from: dt.datetime | None = None,
                      date_to: dt.datetime | None = None) -> tuple[list[Call], int]:
    """Filtered, paginated call log. Returns (rows, total_matching)."""
    q = session.query(Call)
    if outcome:
        q = q.filter(Call.outcome == outcome)
    if date_from:
        q = q.filter(Call.ended_at >= date_from)
    if date_to:
        q = q.filter(Call.ended_at < date_to)
    total = q.count()
    # Order by call time, not insert order — "Sync History" inserts old calls
    # with high ids, which would otherwise float to the top.
    rows = (q.order_by(Call.ended_at.desc().nullslast(), Call.id.desc())
            .offset(offset).limit(limit).all())
    return rows, total


def list_upcoming_appointments(session, limit: int = 100) -> list[Appointment]:
    now = dt.datetime.now(dt.timezone.utc)
    return (session.query(Appointment)
            .filter(Appointment.start_utc >= now)
            .filter(Appointment.status.notin_(["cancelled", "noshow", "invalid"]))
            .order_by(Appointment.start_utc.asc())
            .limit(limit).all())


def daily_trend(session, days: int = 14) -> list[dict]:
    """Calls + bookings per local-calendar day for the last N days."""
    tz = _clinic_tz()
    local_midnight = dt.datetime.now(tz).replace(hour=0, minute=0,
                                                 second=0, microsecond=0)
    start_utc = (local_midnight - dt.timedelta(days=days - 1)
                 ).astimezone(dt.timezone.utc)
    calls = (session.query(Call.ended_at, Call.booked)
             .filter(Call.ended_at >= start_utc).all())
    buckets: dict[str, dict] = {}
    for i in range(days):
        d = (local_midnight + dt.timedelta(days=i - days + 1)).date()
        buckets[d.isoformat()] = {"date": d.isoformat(), "calls": 0, "booked": 0}
    for ended_at, booked in calls:
        if ended_at is None:
            continue
        if ended_at.tzinfo is None:                      # SQLite drops tzinfo
            ended_at = ended_at.replace(tzinfo=dt.timezone.utc)
        key = ended_at.astimezone(tz).date().isoformat()
        if key in buckets:
            buckets[key]["calls"] += 1
            if booked:
                buckets[key]["booked"] += 1
    return list(buckets.values())


def digest_stats(session, day_local: dt.date | None = None) -> dict:
    """Stats for one local day (default: yesterday) — feeds the daily digest."""
    tz = _clinic_tz()
    if day_local is None:
        day_local = (dt.datetime.now(tz) - dt.timedelta(days=1)).date()
    start_local = dt.datetime.combine(day_local, dt.time.min, tzinfo=tz)
    start = start_local.astimezone(dt.timezone.utc)
    end = (start_local + dt.timedelta(days=1)).astimezone(dt.timezone.utc)
    rows, total = list_recent_calls(session, limit=1000,
                                    date_from=start, date_to=end)
    booked = sum(1 for r in rows if r.booked)
    abandoned = sum(1 for r in rows if r.outcome == "abandoned")
    durations = [r.duration_seconds for r in rows if r.duration_seconds]

    # new patients that day = first-ever appointments created in the window
    day_appts = (session.query(Appointment)
                 .filter(Appointment.created_at >= start)
                 .filter(Appointment.created_at < end).all())
    firsts = first_appointment_ids(session, [a.phone_hash for a in day_appts])
    new_patients = sum(1 for a in day_appts if a.id in firsts)

    return {
        "date": day_local.isoformat(),
        "total_calls": total,
        "booked": booked,
        "abandoned": abandoned,
        "info_given": sum(1 for r in rows if r.outcome == "info_given"),
        "avg_duration_seconds": int(sum(durations) / len(durations)) if durations else 0,
        "new_patients": new_patients,
    }


# ---------- patient registry ----------

def upsert_patient(session, *, phone: str, name: str | None = None,
                   insurance: str | None = None, clinic_id: int = 1,
                   first_seen_at: dt.datetime | None = None) -> Patient | None:
    """Find-or-create the patient for this phone. Fills in name/insurance
    when they arrive (bookings know the name; plain calls may not)."""
    ph = crypto.phone_hash(phone) if phone else None
    if not ph:
        return None
    patient = session.query(Patient).filter_by(phone_hash=ph).one_or_none()
    if patient is None:
        patient = Patient(phone_hash=ph, phone_enc=crypto.encrypt(phone),
                          clinic_id=clinic_id,
                          first_seen_at=first_seen_at or dt.datetime.now(dt.timezone.utc))
        session.add(patient)
        session.flush()          # assign id before linking
    if name and name.lower() not in ("the caller", "unknown"):
        patient.name_enc = crypto.encrypt(name)
    if insurance:
        patient.insurance_enc = crypto.encrypt(insurance)
    if first_seen_at is not None and patient.first_seen_at is not None:
        fs = patient.first_seen_at
        if fs.tzinfo is None:
            fs = fs.replace(tzinfo=dt.timezone.utc)
        if first_seen_at < fs:
            patient.first_seen_at = first_seen_at
    return patient


def backfill_patients(session_factory) -> dict:
    """One-time (idempotent) link of historical calls/appointments to the
    registry. Runs at startup; only touches rows with patient_id IS NULL."""
    linked_calls = linked_appts = created_before = 0
    with session_factory() as session:
        created_before = session.query(Patient).count()
        # appointments first — they carry names
        for a in (session.query(Appointment)
                  .filter(Appointment.patient_id.is_(None))
                  .filter(Appointment.phone_hash.isnot(None)).all()):
            phone = None
            try:
                phone = crypto.decrypt(a.caller_phone_enc)
            except Exception:
                pass
            if not phone:
                continue
            name = None
            try:
                name = crypto.decrypt(a.caller_name_enc)
            except Exception:
                pass
            created = a.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=dt.timezone.utc)
            p = upsert_patient(session, phone=phone, name=name,
                               first_seen_at=created)
            if p:
                a.patient_id = p.id
                linked_appts += 1
        for c in (session.query(Call)
                  .filter(Call.patient_id.is_(None))
                  .filter(Call.phone_hash.isnot(None)).all()):
            phone = None
            try:
                phone = crypto.decrypt(c.phone_enc)
            except Exception:
                pass
            if not phone:
                continue
            ended = c.ended_at
            if ended is not None and ended.tzinfo is None:
                ended = ended.replace(tzinfo=dt.timezone.utc)
            p = upsert_patient(session, phone=phone, first_seen_at=ended)
            if p:
                c.patient_id = p.id
                linked_calls += 1
        session.commit()
        total = session.query(Patient).count()
    return {"patients_created": total - created_before,
            "calls_linked": linked_calls, "appointments_linked": linked_appts}


def list_patients(session, search: str = "", offset: int = 0,
                  limit: int = 50) -> tuple[list[dict], int]:
    """Patient list with per-patient stats. Names are encrypted, so search
    decrypts in Python — fine at clinic scale (hundreds of rows)."""
    from sqlalchemy import func
    patients = (session.query(Patient)
                .order_by(Patient.first_seen_at.desc()).all())
    call_counts = dict(session.query(Call.patient_id, func.count(Call.id))
                       .filter(Call.patient_id.isnot(None))
                       .group_by(Call.patient_id).all())
    appt_counts = dict(session.query(Appointment.patient_id,
                                     func.count(Appointment.id))
                       .filter(Appointment.patient_id.isnot(None))
                       .group_by(Appointment.patient_id).all())
    q = (search or "").strip().lower()
    rows = []
    for p in patients:
        name = phone = None
        try:
            name = crypto.decrypt(p.name_enc)
        except Exception:
            pass
        try:
            phone = crypto.decrypt(p.phone_enc)
        except Exception:
            pass
        if q and q not in (name or "").lower() and q not in (phone or ""):
            continue
        fs = p.first_seen_at
        rows.append({
            "id": p.id,
            "name": name or "(name unknown)",
            "phone_masked": ("****" + phone[-4:]) if phone and len(phone) >= 4 else "****",
            "insurance": _try_decrypt(p.insurance_enc),
            "calls": call_counts.get(p.id, 0),
            "appointments": appt_counts.get(p.id, 0),
            "first_seen_at": fs.isoformat() if fs else None,
        })
    total = len(rows)
    return rows[offset:offset + limit], total


def _try_decrypt(enc) -> str | None:
    if not enc:
        return None
    try:
        return crypto.decrypt(enc)
    except Exception:
        return None


def patient_profile(session, patient_id: int) -> dict | None:
    """Full profile: identity + entire call and appointment history.
    Shows the full phone number — staff need it for callbacks; access is
    already gated by the dashboard token and audited."""
    p = session.get(Patient, patient_id)
    if p is None:
        return None
    calls = (session.query(Call).filter_by(patient_id=patient_id)
             .order_by(Call.ended_at.desc().nullslast()).limit(200).all())
    appts = (session.query(Appointment).filter_by(patient_id=patient_id)
             .order_by(Appointment.start_utc.desc()).limit(200).all())
    waitlist = (session.query(WaitlistEntry).filter_by(patient_id=patient_id)
                .order_by(WaitlistEntry.id.desc()).limit(20).all())
    write_audit(session, actor="dashboard", action="patient.profile_viewed",
                phi=True, detail={"patient_id": patient_id})
    session.commit()
    fs = p.first_seen_at
    now = dt.datetime.now(dt.timezone.utc)

    def _aw(d):
        return d.replace(tzinfo=dt.timezone.utc) if (d and d.tzinfo is None) else d
    past = [a for a in appts if _aw(a.start_utc) and _aw(a.start_utc) < now]
    visits = sum(1 for a in past
                 if a.status not in ("cancelled", "invalid", "noshow", "no_show"))
    no_shows = sum(1 for a in past if a.status in ("noshow", "no_show"))
    upcoming = sum(1 for a in appts
                   if _aw(a.start_utc) and _aw(a.start_utc) >= now
                   and a.status == "confirmed")
    return {
        "id": p.id,
        "name": _try_decrypt(p.name_enc) or "(name unknown)",
        "phone": _try_decrypt(p.phone_enc),
        "insurance": _try_decrypt(p.insurance_enc),
        "dob": _try_decrypt(p.dob_enc),
        "notes": _try_decrypt(p.notes_enc),
        "first_seen_at": fs.isoformat() if fs else None,
        "stats": {"total_calls": len(calls), "visits": visits,
                  "no_shows": no_shows, "upcoming": upcoming},
        "waitlist": [{
            "id": w.id, "service": w.service, "status": w.status,
            "preferred_day": w.preferred_day, "time_note": w.time_note,
        } for w in waitlist],
        "calls": [{
            "call_id": c.call_id,
            "ended_at": c.ended_at.isoformat() if c.ended_at else None,
            "outcome": c.outcome,
            "duration_seconds": c.duration_seconds,
            "summary": _try_decrypt(c.summary_enc),
        } for c in calls],
        "appointments": [{
            "id": a.id,
            "service": a.service,
            "start_utc": a.start_utc.isoformat() if a.start_utc else None,
            "status": a.status,
            "reason": _try_decrypt(a.reason_enc),
            "insurance": _try_decrypt(a.insurance_enc),
            "reminder_sent": bool(a.reminder_sent),
        } for a in appts],
    }


def set_patient_notes(session, patient_id: int, notes: str) -> bool:
    p = session.get(Patient, patient_id)
    if p is None:
        return False
    p.notes_enc = crypto.encrypt(notes or None)
    write_audit(session, actor="dashboard", action="patient.notes_updated",
                phi=True, detail={"patient_id": patient_id})
    return True


def find_patient_by_phone(session, phone: str) -> Patient | None:
    ph = crypto.phone_hash(phone) if phone else None
    if not ph:
        return None
    return session.query(Patient).filter_by(phone_hash=ph).one_or_none()


def next_appointment_for_phone(session, phone: str) -> Appointment | None:
    ph = crypto.phone_hash(phone) if phone else None
    if not ph:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    return (session.query(Appointment)
            .filter(Appointment.phone_hash == ph)
            .filter(Appointment.start_utc >= now)
            .filter(Appointment.status == "confirmed")
            .order_by(Appointment.start_utc.asc()).first())


# ---------- waitlist ----------

def add_waitlist_entry(session, *, phone: str, name: str | None, service: str,
                       preferred_day: str | None = None,
                       time_note: str | None = None,
                       call_id: str | None = None) -> WaitlistEntry:
    patient = upsert_patient(session, phone=phone, name=name)
    entry = WaitlistEntry(
        patient_id=patient.id if patient else None,
        call_id=call_id,
        phone_hash=crypto.phone_hash(phone),
        phone_enc=crypto.encrypt(phone),
        name_enc=crypto.encrypt(name),
        service=service,
        preferred_day=preferred_day or None,
        time_note=time_note or None)
    session.add(entry)
    write_audit(session, actor="voice_ai" if call_id else "dashboard",
                action="waitlist.added", call_id=call_id, phi=True,
                detail={"service": service, "preferred_day": preferred_day})
    return entry


def list_waitlist(session, include_closed: bool = False) -> list[dict]:
    q = session.query(WaitlistEntry)
    if not include_closed:
        q = q.filter(WaitlistEntry.status.in_(["waiting", "offered"]))
    rows = []
    for w in q.order_by(WaitlistEntry.id.asc()).limit(200).all():
        phone = _try_decrypt(w.phone_enc)
        rows.append({
            "id": w.id,
            "patient_id": w.patient_id,
            "name": _try_decrypt(w.name_enc) or "(name unknown)",
            "phone_masked": ("****" + phone[-4:]) if phone and len(phone) >= 4 else "****",
            "service": w.service,
            "preferred_day": w.preferred_day,
            "time_note": w.time_note,
            "status": w.status,
            "offered_detail": w.offered_detail,
            "created_at": w.created_at.isoformat() if w.created_at else None,
        })
    return rows


def match_waitlist(session, *, service: str | None,
                   day: str | None) -> WaitlistEntry | None:
    """Oldest waiting entry matching a freed slot. Preference order:
    exact service+day, then service+any-day, then any-service+day, then any."""
    base = (session.query(WaitlistEntry)
            .filter(WaitlistEntry.status == "waiting")
            .order_by(WaitlistEntry.id.asc()))
    svc = (service or "").lower()
    candidates = base.all()

    def rank(w) -> int:
        s_match = bool(svc) and (w.service or "").lower() == svc
        d_match = day is not None and w.preferred_day == day
        d_any = w.preferred_day is None
        if s_match and (d_match or d_any):
            return 0 if d_match else 1
        if d_match or d_any:
            return 2 if d_match else 3
        return 9
    ranked = sorted((w for w in candidates if rank(w) < 9),
                    key=lambda w: (rank(w), w.id))
    return ranked[0] if ranked else None


def set_waitlist_status(session, entry_id: int, status: str,
                        offered_detail: str | None = None) -> bool:
    w = session.get(WaitlistEntry, entry_id)
    if w is None:
        return False
    w.status = status
    if status == "offered":
        w.offered_at = dt.datetime.now(dt.timezone.utc)
        w.offered_detail = offered_detail
    write_audit(session, actor="system", action=f"waitlist.{status}",
                phi=False, detail={"entry_id": entry_id,
                                   "offered_detail": offered_detail})
    return True


# ---------- patient editing & merge (registry accuracy) ----------

_PATIENT_EDITABLE = {"name": "name_enc", "insurance": "insurance_enc",
                     "dob": "dob_enc", "notes": "notes_enc"}


def update_patient(session, patient_id: int, fields: dict) -> bool:
    p = session.get(Patient, patient_id)
    if p is None:
        return False
    changed = []
    for key, col in _PATIENT_EDITABLE.items():
        if key in fields:
            value = (str(fields[key]).strip() or None) if fields[key] is not None else None
            setattr(p, col, crypto.encrypt(value))
            changed.append(key)
    if changed:
        write_audit(session, actor="dashboard", action="patient.updated",
                    phi=True, detail={"patient_id": patient_id, "fields": changed})
    return True


def merge_patients(session, target_id: int, source_id: int) -> dict:
    """Merge source into target (same person, two phone numbers). Relinks all
    history; keeps target's phone; fills gaps from source; deletes source."""
    if target_id == source_id:
        return {"ok": False, "error": "cannot merge a patient into itself"}
    target = session.get(Patient, target_id)
    source = session.get(Patient, source_id)
    if target is None or source is None:
        return {"ok": False, "error": "patient not found"}

    moved_calls = (session.query(Call).filter_by(patient_id=source_id)
                   .update({"patient_id": target_id}))
    moved_appts = (session.query(Appointment).filter_by(patient_id=source_id)
                   .update({"patient_id": target_id}))
    moved_wait = (session.query(WaitlistEntry).filter_by(patient_id=source_id)
                  .update({"patient_id": target_id}))
    # fill gaps on target from source
    for col in ("name_enc", "insurance_enc", "dob_enc"):
        if getattr(target, col) is None and getattr(source, col) is not None:
            setattr(target, col, getattr(source, col))
    src_notes = _try_decrypt(source.notes_enc)
    if src_notes:
        tgt_notes = _try_decrypt(target.notes_enc) or ""
        merged = (tgt_notes + "\n" if tgt_notes else "") + f"[merged] {src_notes}"
        target.notes_enc = crypto.encrypt(merged)
    # keep earliest first_seen
    fs_t, fs_s = target.first_seen_at, source.first_seen_at
    if fs_s is not None:
        if fs_s.tzinfo is None:
            fs_s = fs_s.replace(tzinfo=dt.timezone.utc)
        if fs_t is None:
            target.first_seen_at = fs_s
        else:
            if fs_t.tzinfo is None:
                fs_t = fs_t.replace(tzinfo=dt.timezone.utc)
            target.first_seen_at = min(fs_t, fs_s)
    session.delete(source)
    write_audit(session, actor="dashboard", action="patient.merged", phi=True,
                detail={"target": target_id, "source": source_id,
                        "calls": moved_calls, "appointments": moved_appts})
    return {"ok": True, "calls_moved": moved_calls,
            "appointments_moved": moved_appts, "waitlist_moved": moved_wait}


def find_appointment_by_ghl_id(session, ghl_id: str) -> Appointment | None:
    if not ghl_id:
        return None
    return (session.query(Appointment)
            .filter_by(calcom_booking_uid=str(ghl_id))
            .order_by(Appointment.id.desc()).first())


# ---------- new-patient identification ----------
# "New patient" is computed from history rather than stored, so it stays
# correct no matter what order rows were inserted (live webhooks vs. the
# "Sync History" backfill, which inserts newest-first).
#
#   new caller  (per call):        this phone_hash's earliest call
#   new patient (per appointment): this phone_hash's earliest appointment

def first_appointment_ids(session, phone_hashes: list[str]) -> set[int]:
    """Return Appointment.ids that are the FIRST appointment for their phone."""
    from sqlalchemy import func
    hashes = [h for h in set(phone_hashes) if h]
    if not hashes:
        return set()
    rows = (session.query(Appointment.phone_hash,
                          func.min(Appointment.id))
            .filter(Appointment.phone_hash.in_(hashes))
            .group_by(Appointment.phone_hash).all())
    return {first_id for _, first_id in rows}


def first_call_ids(session, phone_hashes: list[str]) -> set[int]:
    """Return Call.ids that are the FIRST call from their phone (by ended_at)."""
    from sqlalchemy import func, tuple_
    hashes = [h for h in set(phone_hashes) if h]
    if not hashes:
        return set()
    # earliest (ended_at, id) per hash; id tiebreak for simultaneous events
    sub = (session.query(Call.phone_hash.label("h"),
                         func.min(Call.ended_at).label("first_end"))
           .filter(Call.phone_hash.in_(hashes))
           .group_by(Call.phone_hash).subquery())
    rows = (session.query(func.min(Call.id))
            .join(sub, (Call.phone_hash == sub.c.h)
                  & (Call.ended_at == sub.c.first_end))
            .group_by(Call.phone_hash).all())
    return {r[0] for r in rows}


def monthly_new_patients(session, months: int = 6) -> list[dict]:
    """New patients per local calendar month (first-ever appointment)."""
    from sqlalchemy import func
    tz = _clinic_tz()
    rows = (session.query(Appointment.phone_hash,
                          func.min(Appointment.created_at))
            .filter(Appointment.phone_hash.isnot(None))
            .filter(Appointment.status.notin_(["cancelled", "invalid"]))
            .group_by(Appointment.phone_hash).all())
    now_local = dt.datetime.now(tz)
    buckets: dict[str, int] = {}
    keys: list[str] = []
    y, m = now_local.year, now_local.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    for k in keys:
        buckets[k] = 0
    for _hash, first_created in rows:
        if first_created is None:
            continue
        if first_created.tzinfo is None:                 # SQLite drops tzinfo
            first_created = first_created.replace(tzinfo=dt.timezone.utc)
        key = first_created.astimezone(tz).strftime("%Y-%m")
        if key in buckets:
            buckets[key] += 1
    return [{"month": k, "new_patients": buckets[k]} for k in keys]


def appointments_needing_reminder(session, tomorrow_local: dt.date) -> list[Appointment]:
    """Confirmed appointments starting tomorrow (local) with no reminder sent."""
    tz = _clinic_tz()
    start_local = dt.datetime.combine(tomorrow_local, dt.time.min, tzinfo=tz)
    start = start_local.astimezone(dt.timezone.utc)
    end = (start_local + dt.timedelta(days=1)).astimezone(dt.timezone.utc)
    return (session.query(Appointment)
            .filter(Appointment.start_utc >= start)
            .filter(Appointment.start_utc < end)
            .filter(Appointment.status == "confirmed")
            .filter((Appointment.reminder_sent == False)      # noqa: E712
                    | (Appointment.reminder_sent.is_(None)))
            .all())


def record_failed_event(session, *, source: str, call_id: str | None,
                        payload: dict, error: str) -> FailedEvent:
    ev = FailedEvent(source=source, call_id=call_id,
                     payload=payload, error=error[:500])
    session.add(ev)
    return ev


def list_failed_events(session, include_replayed: bool = False,
                       limit: int = 100) -> list[FailedEvent]:
    q = session.query(FailedEvent)
    if not include_replayed:
        q = q.filter(FailedEvent.replayed == False)  # noqa: E712
    return q.order_by(FailedEvent.id.desc()).limit(limit).all()


def get_kpis(session) -> dict:
    from sqlalchemy import func
    import datetime as dt
    import os
    from zoneinfo import ZoneInfo

    # "Today" / "this week" in the clinic's local timezone, not UTC —
    # otherwise evening calls count toward tomorrow.
    tz = ZoneInfo(os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis"))
    local_midnight = dt.datetime.now(tz).replace(hour=0, minute=0,
                                                 second=0, microsecond=0)
    today_start = local_midnight.astimezone(dt.timezone.utc)
    week_start = (local_midnight
                  - dt.timedelta(days=local_midnight.weekday())
                  ).astimezone(dt.timezone.utc)

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
    monthly = monthly_new_patients(session, months=6)
    return {
        "calls_today": calls_today,
        "calls_this_week": calls_this_week,
        "total_calls": total,
        "booked_total": booked_total,
        "booking_rate_pct": round(booked_total / total * 100, 1) if total else 0,
        "avg_duration_seconds": int(avg_dur),
        "outcome_breakdown": {o: c for o, c in outcome_rows},
        "new_patients_this_month": monthly[-1]["new_patients"] if monthly else 0,
        "monthly_new_patients": monthly,
    }
