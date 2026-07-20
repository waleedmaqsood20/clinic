"""
Staff dashboard — token-protected /dashboard (HTML) + JSON APIs.

Endpoints (all require DASHBOARD_TOKEN via x-dashboard-token header or ?token=):
  GET  /dashboard                     HTML page
  GET  /api/kpis                      KPI cards + outcome breakdown + 14-day trend
  GET  /api/calls                     filtered/paginated call log (decrypted)
  GET  /api/calls.csv                 CSV export of the filtered call log
  GET  /api/appointments              upcoming appointments (decrypted names)
  GET  /api/recording/{call_id}       307 → fresh Retell recording URL
  GET  /api/failed-events             dead-lettered webhook payloads
  POST /api/failed-events/{id}/replay retry persisting a dead-lettered call
  GET  /api/digest                    preview yesterday's digest text
  POST /api/send-digest               send the digest via SMS (DIGEST_TO_NUMBER)
  POST /api/sync-retell               pull full call history from Retell

Phone numbers are masked to the last 4 digits. Summaries and transcripts are
decrypted for authenticated viewers. Set DASHBOARD_TOKEN in .env before using.
"""
from __future__ import annotations
import csv
import datetime as dt
import hmac
import io
import os
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import crypto, repository, call_tracking

# Applied to every dashboard response: PHI must never be cached, and the page
# must not be frameable (clickjacking) or sniffed.
_SEC_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# The dashboard is a single inline-scripted page, so 'unsafe-inline' is required.
# media-src allows the recording redirect target (Retell S3 presigned URLs).
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self'; "
        "media-src 'self' https:; img-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

# "Needs attention" keyword list — matched against summary + transcript.
# Deliberately excludes "cancel" (the agent handles cancellations normally).
_ATTENTION_KEYWORDS = ("call me back", "call back", "complaint", "unhappy",
                       "frustrat", "emergency", "urgent", "severe pain",
                       "manager", "billing issue")



def _clinic_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis"))


def _mask_phone(enc: bytes | None) -> str:
    if not enc:
        return "****"
    try:
        phone = crypto.decrypt(enc) or ""
        return ("****" + phone[-4:]) if len(phone) >= 4 else "****"
    except Exception:
        return "****"


def _decrypt_or_none(enc: bytes | None) -> str | None:
    if not enc:
        return None
    try:
        return crypto.decrypt(enc)
    except Exception:
        return None


def _attention_reasons(outcome: str | None, duration: int | None,
                       summary: str | None, transcript: str | None) -> list[str]:
    reasons: list[str] = []
    if outcome == "abandoned":
        reasons.append("abandoned call")
    if duration is not None and 0 < duration < 15:
        reasons.append("very short call")
    text = f"{summary or ''} {transcript or ''}".lower()
    for kw in _ATTENTION_KEYWORDS:
        if kw in text:
            reasons.append(f'mentions "{kw}"')
            if len(reasons) >= 4:
                break
    return reasons


def _parse_local_date(value: str | None, end_of_day: bool = False) -> dt.datetime | None:
    """YYYY-MM-DD in clinic-local time → aware UTC datetime (exclusive end)."""
    if not value:
        return None
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"bad date: {value!r} (expected YYYY-MM-DD)")
    local = dt.datetime.combine(day, dt.time.min, tzinfo=_clinic_tz())
    if end_of_day:
        local += dt.timedelta(days=1)
    return local.astimezone(dt.timezone.utc)


def _call_row(c, new_caller: bool = False) -> dict:
    summary = _decrypt_or_none(c.summary_enc)
    transcript = _decrypt_or_none(c.transcript_enc)
    reasons = _attention_reasons(c.outcome, c.duration_seconds, summary, transcript)
    if getattr(c, "booking_verified", None) is False:
        reasons.insert(0, "booked but no matching GHL appointment")
    return {
        "call_id": c.call_id,
        "phone": _mask_phone(c.phone_enc),
        "outcome": c.outcome,
        "duration_seconds": c.duration_seconds,
        "booked": c.booked,
        "summary": summary,
        "transcript": transcript,
        "recording_ref": c.recording_ref,
        "cost_usd": c.cost_usd,
        "ended_at": c.ended_at.isoformat() if c.ended_at else None,
        "attention": bool(reasons),
        "attention_reasons": reasons,
        "new_caller": new_caller,
        "booking_verified": getattr(c, "booking_verified", None),
    }


def _call_filters(request: Request) -> dict:
    outcome = request.query_params.get("outcome") or None
    return {
        "outcome": outcome,
        "date_from": _parse_local_date(request.query_params.get("from")),
        "date_to": _parse_local_date(request.query_params.get("to"), end_of_day=True),
    }


def make_dashboard_router(session_factory, sms_provider=None,
                          calendar=None) -> APIRouter:
    router = APIRouter()

    def _auth(request: Request) -> dict:
        """Authenticate via JWT cookie (browser) or legacy DASHBOARD_TOKEN (cron/API).
        Returns {'role', 'user_id', 'jti', 'actor'}."""
        from . import auth as auth_mod
        cookie = request.cookies.get(auth_mod.SESSION_COOKIE)
        if cookie:
            try:
                with session_factory() as _s:
                    sess = auth_mod.verify_token(cookie, _s)
            except Exception:
                sess = None
            if sess:
                return {"role": sess["role"], "user_id": sess["user_id"],
                        "jti": sess["jti"],
                        "actor": f"user:{sess['user_id']}"}
        # Legacy token — kept for cron jobs and API scripts
        token = os.getenv("DASHBOARD_TOKEN")
        if not token:
            raise HTTPException(503, "Dashboard not configured — set DASHBOARD_TOKEN")
        sent = (request.headers.get("x-dashboard-token")
                or request.query_params.get("token") or "")
        if not hmac.compare_digest(sent, token):
            raise HTTPException(401, "unauthorized")
        return {"role": "admin", "user_id": None, "jti": None, "actor": "token"}

    def _require_admin(request: Request) -> dict:
        ctx = _auth(request)
        if ctx["role"] != "admin":
            raise HTTPException(403, "admin only")
        return ctx

    @router.post("/api/sync-retell")
    async def api_sync_retell(request: Request):
        _auth(request)
        api_key = os.getenv("RETELL_API_KEY")
        if not api_key:
            raise HTTPException(503, "RETELL_API_KEY not set")
        import asyncio
        result = await asyncio.to_thread(
            call_tracking.sync_from_retell_api, session_factory, api_key)
        return JSONResponse(content=result, headers=_SEC_HEADERS)

    @router.post("/api/sync-ghl-appointments")
    async def api_sync_ghl_appointments(request: Request):
        _auth(request)
        if calendar is None:
            raise HTTPException(503, "GHL calendar not configured")
        import asyncio
        result = await asyncio.to_thread(
            call_tracking.sync_ghl_appointments, session_factory, calendar)
        return JSONResponse(content=result, headers=_SEC_HEADERS)

    @router.get("/api/debug-booking")
    async def api_debug_booking(request: Request):
        """Diagnostic: shows exactly what's in DB and what GHL returns."""
        _auth(request)
        from . import crypto
        from .models import Call, Appointment
        from sqlalchemy import func as sqlfunc

        result: dict = {}

        with session_factory() as session:
            total_calls  = session.query(sqlfunc.count(Call.id)).scalar() or 0
            booked_calls = session.query(sqlfunc.count(Call.id)).filter(Call.booked == True).scalar() or 0
            total_appts  = session.query(sqlfunc.count(Appointment.id)).scalar() or 0
            linked_appts = session.query(sqlfunc.count(Appointment.id)).filter(Appointment.call_id.isnot(None)).scalar() or 0

            sample_calls = session.query(Call).order_by(Call.ended_at.desc()).limit(5).all()
            sample_appts = session.query(Appointment).order_by(Appointment.id.desc()).limit(5).all()

            result["db"] = {
                "total_calls": total_calls,
                "booked_calls": booked_calls,
                "total_appointments": total_appts,
                "linked_appointments": linked_appts,
                "sample_calls": [
                    {"call_id": c.call_id, "phone_hash_prefix": (c.phone_hash or "")[:12],
                     "booked": c.booked, "outcome": c.outcome,
                     "ended_at": c.ended_at.isoformat() if c.ended_at else None}
                    for c in sample_calls
                ],
                "sample_appointments": [
                    {"id": a.id, "call_id": a.call_id,
                     "phone_hash_prefix": (a.phone_hash or "")[:12],
                     "status": a.status,
                     "start_utc": a.start_utc.isoformat() if a.start_utc else None,
                     "ghl_id": a.calcom_booking_uid}
                    for a in sample_appts
                ],
            }

        if calendar is None:
            result["ghl"] = {"error": "calendar not configured"}
        else:
            try:
                events = calendar.fetch_calendar_events_range(days_back=120, days_ahead=90)
                result["ghl"] = {"events_fetched": len(events), "sample": []}
                for ev in events[:5]:
                    contact_id = ev.get("contactId")
                    try:
                        raw   = calendar._get_contact_phone(contact_id) if contact_id else None
                        e164  = calendar._to_e164(raw) if raw else None
                        ph    = crypto.phone_hash(e164) if e164 else None
                    except Exception as ex:
                        raw = e164 = ph = f"ERR:{ex}"
                    with session_factory() as session:
                        call_match = (
                            session.query(Call).filter(Call.phone_hash == ph).first()
                            if ph and isinstance(ph, str) else None
                        )
                    result["ghl"]["sample"].append({
                        "event_id": ev.get("id"),
                        "status": ev.get("appointmentStatus"),
                        "start_ms": ev.get("startTime"),
                        "contact_id": contact_id,
                        "phone_raw": raw,
                        "phone_e164": e164,
                        "phone_hash_prefix": ph[:12] if ph and isinstance(ph, str) else ph,
                        "call_found_in_db": call_match is not None,
                        "matching_call_id": call_match.call_id if call_match else None,
                        "matching_call_booked": call_match.booked if call_match else None,
                    })
            except Exception as ex:
                result["ghl"] = {"error": str(ex)}

        return JSONResponse(content=result, headers=_SEC_HEADERS)

    @router.get("/api/kpis")
    async def api_kpis(request: Request):
        _auth(request)
        from . import analytics
        tz = _clinic_tz()
        month_start = dt.datetime.now(tz).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).astimezone(dt.timezone.utc)
        with session_factory() as session:
            kpis = repository.get_kpis(session)
            kpis["daily_trend"] = repository.daily_trend(session, days=14)
            kpis["revenue_this_month"] = analytics.revenue_stats(
                session, start=month_start)["estimated_total"]
        return JSONResponse(content=kpis, headers=_SEC_HEADERS)

    @router.get("/api/calls")
    async def api_calls(request: Request):
        _auth(request)
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
            limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        except ValueError:
            raise HTTPException(400, "offset/limit must be integers")
        filters = _call_filters(request)
        with session_factory() as session:
            calls, total = repository.list_recent_calls(
                session, limit=limit, offset=offset, **filters)
            firsts = repository.first_call_ids(
                session, [c.phone_hash for c in calls])
            rows = [_call_row(c, new_caller=c.id in firsts) for c in calls]
        return JSONResponse(
            content={"rows": rows, "total": total,
                     "offset": offset, "limit": limit},
            headers=_SEC_HEADERS)

    @router.get("/api/calls.csv")
    async def api_calls_csv(request: Request):
        _auth(request)
        filters = _call_filters(request)
        with session_factory() as session:
            calls, _ = repository.list_recent_calls(
                session, limit=1000, offset=0, **filters)
            firsts = repository.first_call_ids(
                session, [c.phone_hash for c in calls])
            rows = [_call_row(c, new_caller=c.id in firsts) for c in calls]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ended_at", "phone", "duration_seconds", "outcome",
                    "booked", "booking_verified", "new_caller",
                    "needs_attention", "attention_reasons",
                    "cost_usd", "summary"])
        for r in rows:
            w.writerow([r["ended_at"], r["phone"], r["duration_seconds"],
                        r["outcome"], r["booked"], r["booking_verified"],
                        r["new_caller"], r["attention"],
                        "; ".join(r["attention_reasons"]),
                        r["cost_usd"], r["summary"] or ""])
        stamp = dt.datetime.now(_clinic_tz()).strftime("%Y%m%d-%H%M")
        return Response(
            content=buf.getvalue(), media_type="text/csv",
            headers={**_SEC_HEADERS,
                     "Content-Disposition":
                         f'attachment; filename="calls-{stamp}.csv"'})

    @router.get("/api/appointments")
    async def api_appointments(request: Request):
        _auth(request)
        tz = _clinic_tz()
        with session_factory() as session:
            appts = repository.list_upcoming_appointments(session)
            firsts = repository.first_appointment_ids(
                session, [a.phone_hash for a in appts])
            rows = []
            for a in appts:
                start = a.start_utc
                if start is not None and start.tzinfo is None:  # SQLite drops tz
                    start = start.replace(tzinfo=dt.timezone.utc)
                rows.append({
                    "id": a.id,
                    "start_local": (start.astimezone(tz).isoformat()
                                    if start else None),
                    "service": a.service,
                    "name": _decrypt_or_none(a.caller_name_enc) or "—",
                    "phone": _mask_phone(a.caller_phone_enc),
                    "status": a.status,
                    "ghl_appointment_id": a.calcom_booking_uid,
                    "call_id": a.call_id,
                    "is_new_patient": a.id in firsts,
                    "insurance": _decrypt_or_none(
                        getattr(a, "insurance_enc", None)),
                    "reminder_sent": bool(getattr(a, "reminder_sent", False)),
                })
        return JSONResponse(content=rows, headers=_SEC_HEADERS)

    # ---------- auth ----------

    @router.get("/login", response_class=HTMLResponse)
    async def login_page():
        return HTMLResponse(content=_LOGIN_HTML,
                            headers={**_SEC_HEADERS,
                                     "Content-Security-Policy": _CSP})

    @router.post("/api/login")
    async def api_login(request: Request):
        from . import auth as auth_mod
        body = await request.json()
        key = (request.client.host if request.client else "?")
        with session_factory() as session:
            if auth_mod.throttled(key, session):
                raise HTTPException(429, "too many attempts — try again in 15 minutes")
            user = auth_mod.authenticate(session, body.get("username", ""),
                                         body.get("password", ""))
            if user is None:
                auth_mod.record_failure(key, session)
                repository.write_audit(session, actor="anonymous",
                                       action="auth.login_failed", phi=False,
                                       detail={"username": str(body.get('username', ''))[:40]})
                session.commit()
                raise HTTPException(401, "invalid username or password")
            auth_mod.clear_failures(key, session)
            jwt_token = auth_mod.make_token(user.id, user.role)
            repository.write_audit(session, actor=f"user:{user.id}",
                                   action="auth.login", phi=False,
                                   detail={"username": user.username})
            session.commit()
            role, username = user.role, user.username
        resp = JSONResponse(content={"ok": True, "role": role,
                                     "username": username},
                            headers=_SEC_HEADERS)
        resp.set_cookie(auth_mod.SESSION_COOKIE, jwt_token, httponly=True,
                        samesite="lax", secure=True,
                        max_age=auth_mod.SESSION_HOURS * 3600, path="/")
        return resp

    @router.post("/api/logout")
    async def api_logout(request: Request):
        from . import auth as auth_mod
        try:
            ctx = _auth(request)
            jti = ctx.get("jti")
            if jti:
                with session_factory() as session:
                    auth_mod.revoke_token(jti, session)
                    session.commit()
        except HTTPException:
            pass
        resp = JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)
        resp.delete_cookie(auth_mod.SESSION_COOKIE, path="/",
                           httponly=True, samesite="lax", secure=True)
        return resp

    @router.get("/api/users")
    async def api_users(request: Request):
        _require_admin(request)
        from .models import DashboardUser
        with session_factory() as session:
            users = session.query(DashboardUser).order_by(DashboardUser.id).all()
            rows = [{"id": u.id, "username": u.username, "role": u.role,
                     "active": u.active,
                     "last_login_at": (u.last_login_at.isoformat()
                                       if u.last_login_at else None)}
                    for u in users]
        return JSONResponse(content=rows, headers=_SEC_HEADERS)

    @router.post("/api/users")
    async def api_create_user(request: Request):
        ctx = _require_admin(request)
        from . import auth as auth_mod
        body = await request.json()
        with session_factory() as session:
            try:
                user = auth_mod.create_user(
                    session, username=body.get("username", ""),
                    password=body.get("password", ""),
                    role=body.get("role", "staff"))
            except ValueError as e:
                raise HTTPException(400, str(e))
            repository.write_audit(session, actor=ctx["actor"],
                                   action="auth.user_created", phi=False,
                                   detail={"username": user.username,
                                           "role": user.role})
            session.commit()
            out = {"id": user.id, "username": user.username, "role": user.role}
        return JSONResponse(content=out, headers=_SEC_HEADERS)

    @router.post("/api/users/{user_id}/disable")
    async def api_disable_user(user_id: int, request: Request):
        ctx = _require_admin(request)
        from .models import DashboardUser
        with session_factory() as session:
            user = session.get(DashboardUser, user_id)
            if user is None:
                raise HTTPException(404, "user not found")
            user.active = False
            repository.write_audit(session, actor=ctx["actor"],
                                   action="auth.user_disabled", phi=False,
                                   detail={"username": user.username})
            session.commit()
        return JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)

    @router.post("/api/users/{user_id}/enable")
    async def api_enable_user(user_id: int, request: Request):
        ctx = _require_admin(request)
        from .models import DashboardUser
        with session_factory() as session:
            user = session.get(DashboardUser, user_id)
            if user is None:
                raise HTTPException(404, "user not found")
            user.active = True
            repository.write_audit(session, actor=ctx["actor"],
                                   action="auth.user_enabled", phi=False,
                                   detail={"username": user.username})
            session.commit()
        return JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)

    @router.post("/api/users/{user_id}/password")
    async def api_reset_user_password(user_id: int, request: Request):
        ctx = _require_admin(request)
        from . import auth as auth_mod
        body = await request.json()
        with session_factory() as session:
            try:
                auth_mod.reset_password(session, user_id, body.get("password", ""))
            except ValueError as e:
                raise HTTPException(400, str(e))
            repository.write_audit(session, actor=ctx["actor"],
                                   action="auth.password_reset", phi=False,
                                   detail={"user_id": user_id})
            session.commit()
        return JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)

    @router.post("/api/users/{user_id}/role")
    async def api_change_user_role(user_id: int, request: Request):
        ctx = _require_admin(request)
        from .models import DashboardUser
        body = await request.json()
        new_role = body.get("role", "")
        if new_role not in ("admin", "staff"):
            raise HTTPException(400, "role must be admin or staff")
        with session_factory() as session:
            user = session.get(DashboardUser, user_id)
            if user is None:
                raise HTTPException(404, "user not found")
            old_role = user.role
            user.role = new_role
            repository.write_audit(session, actor=ctx["actor"],
                                   action="auth.role_changed", phi=False,
                                   detail={"username": user.username,
                                           "from": old_role, "to": new_role})
            session.commit()
        return JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)

    @router.post("/api/me/password")
    async def api_change_my_password(request: Request):
        from . import auth as auth_mod
        ctx = _auth(request)
        if ctx["user_id"] is None:
            raise HTTPException(403, "log in with a user account to change your password")
        body = await request.json()
        with session_factory() as session:
            try:
                auth_mod.change_password(session, ctx["user_id"],
                                         body.get("current_password", ""),
                                         body.get("new_password", ""))
            except ValueError as e:
                raise HTTPException(400, str(e))
            repository.write_audit(session, actor=ctx["actor"],
                                   action="auth.password_changed", phi=False)
            session.commit()
        # Revoke current JWT so they must log in again with the new password
        jti = ctx.get("jti")
        if jti:
            with session_factory() as session:
                auth_mod.revoke_token(jti, session)
                session.commit()
        resp = JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)
        resp.delete_cookie(auth_mod.SESSION_COOKIE, path="/",
                           httponly=True, samesite="lax", secure=True)
        return resp

    # ---------- waitlist ----------

    @router.get("/api/waitlist")
    async def api_waitlist(request: Request):
        _auth(request)
        include_closed = request.query_params.get("all") == "1"
        with session_factory() as session:
            rows = repository.list_waitlist(session,
                                            include_closed=include_closed)
        return JSONResponse(content=rows, headers=_SEC_HEADERS)

    @router.post("/api/waitlist/{entry_id}/remove")
    async def api_waitlist_remove(entry_id: int, request: Request):
        _auth(request)
        with session_factory() as session:
            ok = repository.set_waitlist_status(session, entry_id, "removed")
            session.commit()
        if not ok:
            raise HTTPException(404, "entry not found")
        return JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)

    @router.post("/api/waitlist/{entry_id}/offer")
    async def api_waitlist_offer(entry_id: int, request: Request):
        """Manual nudge: SMS the waitlisted patient that slots have opened."""
        _auth(request)
        from . import crypto
        from .models import WaitlistEntry
        from .knowledge import CLINIC_PROFILE
        with session_factory() as session:
            w = session.get(WaitlistEntry, entry_id)
            if w is None:
                raise HTTPException(404, "entry not found")
            phone = crypto.decrypt(w.phone_enc)
            service = w.service
        try:
            sms_provider.send(phone,
                              f"{CLINIC_PROFILE['name']}: good news — we have "
                              f"{service or 'appointment'} openings. Call us at "
                              f"{CLINIC_PROFILE['phone']} to grab one!")
        except Exception as exc:
            raise HTTPException(502, f"SMS failed: {type(exc).__name__}")
        with session_factory() as session:
            repository.set_waitlist_status(session, entry_id, "offered",
                                           offered_detail="manual dashboard offer")
            session.commit()
        return JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)

    # ---------- outbound calls ----------

    @router.post("/api/call-patient/{patient_id}")
    async def api_call_patient(patient_id: int, request: Request):
        """Staff-initiated: Sarah calls the patient back."""
        ctx = _auth(request)
        from . import crypto, outbound
        from .models import Patient
        with session_factory() as session:
            p = session.get(Patient, patient_id)
            if p is None:
                raise HTTPException(404, "patient not found")
            phone = crypto.decrypt(p.phone_enc)
            try:
                name = crypto.decrypt(p.name_enc)
            except Exception:
                name = None
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        reason = str(body.get("reason") or "The clinic asked you to follow up "
                     "with this patient.")[:300]
        result = outbound.create_call(
            phone, purpose="callback",
            context=f"Calling {name or 'a patient'}. {reason}")
        if result.get("ok"):
            with session_factory() as session:
                repository.write_audit(session, actor=ctx["actor"],
                                       action="outbound.call_placed", phi=True,
                                       detail={"patient_id": patient_id,
                                               "call_id": result.get("call_id")})
                session.commit()
            return JSONResponse(content=result, headers=_SEC_HEADERS)
        return JSONResponse(content=result, status_code=502,
                            headers=_SEC_HEADERS)

    @router.post("/api/outbound-confirmations")
    async def api_outbound_confirmations(request: Request):
        """Voice-confirm tomorrow's appointments (alternative to SMS reminders)."""
        _auth(request)
        from . import outbound
        import asyncio
        if not outbound.configured():
            raise HTTPException(503, "set RETELL_API_KEY and RETELL_PHONE_NUMBER")
        result = await asyncio.to_thread(
            outbound.confirm_tomorrows_appointments, session_factory)
        return JSONResponse(content=result, headers=_SEC_HEADERS)

    # ---------- patient editing / merge ----------

    @router.put("/api/patients/{patient_id}")
    async def api_update_patient(patient_id: int, request: Request):
        _auth(request)
        body = await request.json()
        fields = {k: body[k] for k in ("name", "insurance", "dob", "notes")
                  if k in body}
        if not fields:
            raise HTTPException(400, "nothing to update")
        with session_factory() as session:
            ok = repository.update_patient(session, patient_id, fields)
            session.commit()
        if not ok:
            raise HTTPException(404, "patient not found")
        return JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)

    @router.post("/api/patients/{target_id}/merge/{source_id}")
    async def api_merge_patients(target_id: int, source_id: int,
                                 request: Request):
        _auth(request)
        with session_factory() as session:
            result = repository.merge_patients(session, target_id, source_id)
            if result.get("ok"):
                session.commit()
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "merge failed"))
        return JSONResponse(content=result, headers=_SEC_HEADERS)

    @router.get("/api/patients")
    async def api_patients(request: Request):
        _auth(request)
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
            limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        except ValueError:
            raise HTTPException(400, "offset/limit must be integers")
        search = request.query_params.get("search", "")
        with session_factory() as session:
            rows, total = repository.list_patients(
                session, search=search, offset=offset, limit=limit)
        return JSONResponse(content={"rows": rows, "total": total,
                                     "offset": offset, "limit": limit},
                            headers=_SEC_HEADERS)

    @router.get("/api/patients/{patient_id}")
    async def api_patient_profile(patient_id: int, request: Request):
        _auth(request)
        with session_factory() as session:
            profile = repository.patient_profile(session, patient_id)
        if profile is None:
            raise HTTPException(404, "patient not found")
        return JSONResponse(content=profile, headers=_SEC_HEADERS)

    @router.put("/api/patients/{patient_id}/notes")
    async def api_patient_notes(patient_id: int, request: Request):
        _auth(request)
        body = await request.json()
        notes = str(body.get("notes", ""))[:4000]
        with session_factory() as session:
            ok = repository.set_patient_notes(session, patient_id, notes)
            session.commit()
        if not ok:
            raise HTTPException(404, "patient not found")
        return JSONResponse(content={"ok": True}, headers=_SEC_HEADERS)

    @router.get("/api/analytics")
    async def api_analytics(request: Request):
        _auth(request)
        from . import analytics
        try:
            days = min(365, max(1, int(request.query_params.get("days", 30))))
        except ValueError:
            raise HTTPException(400, "days must be an integer")
        with session_factory() as session:
            data = analytics.get_analytics(session, days=days)
            data["no_show"] = analytics.no_show_stats(session)
            data["revenue"] = analytics.revenue_stats(session)
        return JSONResponse(content=data, headers=_SEC_HEADERS)

    @router.get("/api/report")
    async def api_report(request: Request):
        _auth(request)
        from . import analytics
        period = request.query_params.get("period", "week")
        if period not in ("week", "month"):
            raise HTTPException(400, "period must be 'week' or 'month'")
        with session_factory() as session:
            report = analytics.build_report(session, period=period)
        return JSONResponse(content=report, headers=_SEC_HEADERS)

    @router.post("/api/send-report")
    async def api_send_report(request: Request):
        _auth(request)
        from . import analytics
        period = request.query_params.get("period", "week")
        to_number = os.getenv("DIGEST_TO_NUMBER")
        with session_factory() as session:
            report = analytics.build_report(session, period=period)
        if not to_number:
            return JSONResponse(content={"sent": False,
                                         "reason": "DIGEST_TO_NUMBER not set",
                                         "text": report["text"]},
                                headers=_SEC_HEADERS)
        try:
            sms_provider.send(to_number, report["text"])
            return JSONResponse(content={"sent": True, "to": to_number,
                                         "text": report["text"]},
                                headers=_SEC_HEADERS)
        except Exception as exc:
            return JSONResponse(content={"sent": False,
                                         "reason": f"{type(exc).__name__}: {exc}",
                                         "text": report["text"]},
                                headers=_SEC_HEADERS)

    @router.post("/api/sync-appointment-statuses")
    async def api_sync_statuses(request: Request):
        _auth(request)
        if calendar is None:
            raise HTTPException(503, "calendar not configured")
        import asyncio
        result = await asyncio.to_thread(
            call_tracking.sync_appointment_statuses, session_factory, calendar)
        return JSONResponse(content=result, headers=_SEC_HEADERS)

    @router.post("/api/send-reminders")
    async def api_send_reminders(request: Request):
        _auth(request)
        from . import scheduler
        import asyncio
        result = await asyncio.to_thread(
            scheduler.send_due_reminders, session_factory, sms_provider)
        return JSONResponse(content=result, headers=_SEC_HEADERS)

    @router.get("/api/recording/{call_id}")
    async def api_recording(call_id: str, request: Request):
        """Redirect to a *fresh* recording URL from Retell.

        Stored recording_ref values are presigned S3 URLs that expire, so old
        rows would silently stop playing. This fetches the current URL per play.
        """
        _auth(request)
        api_key = os.getenv("RETELL_API_KEY")
        if not api_key:
            raise HTTPException(503, "RETELL_API_KEY not set")
        import httpx
        from urllib.parse import quote
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"https://api.retellai.com/v2/get-call/{quote(call_id, safe='')}",
                headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code != 200:
            raise HTTPException(502, "could not fetch call from Retell")
        url = (r.json() or {}).get("recording_url")
        if not url:
            raise HTTPException(404, "no recording available")
        return RedirectResponse(url, status_code=307, headers=_SEC_HEADERS)

    @router.get("/api/failed-events")
    async def api_failed_events(request: Request):
        _auth(request)
        include_replayed = request.query_params.get("all") == "1"
        with session_factory() as session:
            events = repository.list_failed_events(
                session, include_replayed=include_replayed)
            rows = [{
                "id": e.id,
                "source": e.source,
                "call_id": e.call_id,
                "error": e.error,
                "replayed": e.replayed,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            } for e in events]
        return JSONResponse(content=rows, headers=_SEC_HEADERS)

    @router.post("/api/failed-events/{event_id}/replay")
    async def api_replay_failed_event(event_id: int, request: Request):
        _auth(request)
        import asyncio
        result = await asyncio.to_thread(
            call_tracking.replay_failed_event, session_factory, event_id)
        status = 200 if result.get("ok") else 502
        return JSONResponse(content=result, status_code=status,
                            headers=_SEC_HEADERS)

    @router.get("/api/digest")
    async def api_digest_preview(request: Request):
        _auth(request)
        from . import digest
        with session_factory() as session:
            text = digest.build_digest_text(session)
        return JSONResponse(content={"text": text}, headers=_SEC_HEADERS)

    @router.post("/api/send-digest")
    async def api_send_digest(request: Request):
        _auth(request)
        from . import digest
        import asyncio
        result = await asyncio.to_thread(
            digest.send_digest, session_factory, sms_provider)
        return JSONResponse(content=result, headers=_SEC_HEADERS)

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        try:
            _auth(request)
        except HTTPException as e:
            if e.status_code in (401, 503):
                return RedirectResponse("/login", status_code=302,
                                        headers=_SEC_HEADERS)
            raise
        return HTMLResponse(content=_DASHBOARD_HTML,
                            headers={**_SEC_HEADERS,
                                     "Content-Security-Policy": _CSP})

    return router


def _js_escape(s: str) -> str:
    """Escape a value for embedding inside a single-quoted JS string."""
    return (s.replace("\\", "\\\\").replace("'", "\\'")
             .replace("<", "\\x3c").replace(">", "\\x3e")
             .replace("\n", "\\n").replace("\r", "\\r"))


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bright Smile Dental — Sign in</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f5f5f7;color:#1d1d1f;display:flex;align-items:center;
         justify-content:center;min-height:100vh}
    .card{background:#fff;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,.08);
          padding:2.2rem;width:340px}
    h1{font-size:1.05rem;margin-bottom:.25rem}
    .sub{font-size:.8rem;color:#6e6e73;margin-bottom:1.4rem}
    label{font-size:.7rem;color:#6e6e73;text-transform:uppercase;
          letter-spacing:.05em;display:block;margin:.8rem 0 .25rem}
    input{width:100%;padding:.6rem .7rem;border:1px solid #e5e5ea;
          border-radius:8px;font-size:.9rem}
    button{width:100%;margin-top:1.2rem;background:#007aff;color:#fff;border:none;
           border-radius:8px;padding:.65rem;font-size:.9rem;cursor:pointer}
    button:hover{background:#0066d6}
    .err{color:#991b1b;font-size:.8rem;margin-top:.8rem;min-height:1em}
  </style>
</head>
<body>
<div class="card">
  <h1>Bright Smile Dental</h1>
  <div class="sub">Staff dashboard — sign in</div>
  <form id="f">
    <label>Username</label><input id="u" autocomplete="username" required>
    <label>Password</label><input id="p" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
    <div class="err" id="err"></div>
  </form>
</div>
<script>
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const err=document.getElementById('err');
  const btn=document.querySelector('button');
  err.textContent='';
  btn.disabled=true;btn.textContent='Signing in…';
  try{
    const r=await fetch('/api/login',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:document.getElementById('u').value,
                           password:document.getElementById('p').value})});
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      err.textContent=d.detail||('Login failed (HTTP '+r.status+')');
      btn.disabled=false;btn.textContent='Sign in';return;
    }
    window.location='/dashboard';
  }catch(ex){
    err.textContent='Network error — check your connection';
    btn.disabled=false;btn.textContent='Sign in';
  }
});
</script>
</body>
</html>"""


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bright Smile Dental — Call Dashboard</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f5f5f7;color:#1d1d1f;font-size:14px}
    header{background:#fff;border-bottom:1px solid #e5e5ea;padding:1.25rem 2rem;
           display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
    header h1{font-size:1rem;font-weight:600}
    header .sub{font-size:.8rem;color:#6e6e73;margin-top:2px}
    header .spacer{margin-left:auto}
    .btn{background:#f0f0f0;color:#3a3a3c;border:none;border-radius:8px;
         padding:.45rem .9rem;font-size:.8rem;cursor:pointer}
    .btn:hover{background:#e0e0e0}
    .btn:disabled{opacity:.5;cursor:not-allowed}
    .btn-primary{background:#007aff;color:#fff}
    .btn-primary:hover{background:#0066d6}
    .wrap{max-width:1200px;margin:0 auto;padding:1.5rem 2rem}
    .kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
              gap:.875rem;margin-bottom:1.25rem}
    .kpi{background:#fff;border-radius:12px;padding:1.1rem 1.25rem;
         box-shadow:0 1px 3px rgba(0,0,0,.06)}
    .kpi-val{font-size:1.9rem;font-weight:700;line-height:1}
    .kpi-lbl{font-size:.7rem;color:#6e6e73;margin-top:.35rem;
             text-transform:uppercase;letter-spacing:.05em}
    .section{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.06);
             margin-bottom:1.25rem;overflow:hidden}
    .section-hdr{padding:1rem 1.5rem;border-bottom:1px solid #f0f0f0;
                 font-size:.7rem;font-weight:600;color:#6e6e73;
                 text-transform:uppercase;letter-spacing:.06em;
                 display:flex;align-items:center;gap:.75rem;flex-wrap:wrap}
    .outcome-grid{display:flex;flex-wrap:wrap;gap:1rem;padding:1rem 1.5rem}
    .ob{display:flex;align-items:center;gap:.6rem}
    .ob-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
    .ob-name{color:#3a3a3c;text-transform:capitalize}
    .ob-n{font-weight:600;margin-left:.25rem}
    .dot-booked{background:#34c759}
    .dot-info_given{background:#007aff}
    .dot-abandoned{background:#ff9500}
    .dot-other{background:#aeaeb2}
    table{width:100%;border-collapse:collapse}
    thead th{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;
             color:#6e6e73;font-weight:500;padding:.7rem 1.5rem;
             background:#fafafa;border-bottom:1px solid #f0f0f0;text-align:left}
    tbody td{padding:.8rem 1.5rem;border-bottom:1px solid #f5f5f7;vertical-align:top}
    tbody tr:last-child td{border-bottom:none}
    tbody tr:hover td{background:#fafafa}
    .badge{display:inline-block;font-size:.7rem;padding:.2rem .5rem;
           border-radius:4px;font-weight:500;white-space:nowrap}
    .b-booked{background:#d1fae5;color:#065f46}
    .b-info_given{background:#dbeafe;color:#1e40af}
    .b-abandoned{background:#fef3c7;color:#92400e}
    .b-other{background:#f0f0f0;color:#6e6e73}
    .b-attn{background:#fee2e2;color:#991b1b}
    .b-confirmed{background:#d1fae5;color:#065f46}
    .b-new{background:#ede9fe;color:#5b21b6;font-weight:600}
    .b-returning{background:#f0f0f0;color:#6e6e73}
    .b-unverified{background:#fee2e2;color:#991b1b}
    tr.row-new td{background:#faf8ff}
    tr.row-new:hover td{background:#f3f0fc}
    .summary-wrap{max-width:340px}
    .no-sum{color:#aeaeb2;font-style:italic}
    .attn-why{font-size:.7rem;color:#991b1b;margin-top:.25rem}
    .tx-btn{background:none;border:none;color:#007aff;cursor:pointer;
            font-size:.75rem;padding:0;margin-top:.3rem;display:block}
    .tx-box{display:none;margin-top:.4rem;background:#f5f5f7;border-radius:6px;
            padding:.65rem;font-size:.75rem;line-height:1.65;max-height:180px;
            overflow-y:auto;white-space:pre-wrap;color:#3a3a3c}
    .loading{padding:2.5rem;text-align:center;color:#6e6e73}
    .empty{padding:2.5rem;text-align:center;color:#aeaeb2}
    .tabs{display:flex;gap:.25rem;margin-bottom:1rem}
    .tab{background:none;border:none;border-radius:8px;padding:.45rem .9rem;
         font-size:.85rem;cursor:pointer;color:#6e6e73;font-weight:500}
    .tab.active{background:#fff;color:#1d1d1f;box-shadow:0 1px 3px rgba(0,0,0,.06)}
    .filters{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;
             font-size:.75rem;color:#6e6e73;margin-left:auto;font-weight:400;
             text-transform:none;letter-spacing:0}
    .filters select,.filters input[type=date]{font-size:.75rem;padding:.25rem .4rem;
             border:1px solid #e5e5ea;border-radius:6px;background:#fff;color:#3a3a3c}
    .pager{display:flex;gap:.5rem;align-items:center;justify-content:flex-end;
           padding:.6rem 1.5rem;font-size:.75rem;color:#6e6e73;
           border-top:1px solid #f0f0f0}
    .chart-box{padding:1rem 1.5rem}
    .chart-legend{font-size:.7rem;color:#6e6e73;margin-bottom:.5rem;
                  display:flex;gap:1rem}
    .lg{display:flex;align-items:center;gap:.35rem}
    .lg i{width:9px;height:9px;border-radius:2px;display:inline-block}
    .toast{position:fixed;bottom:1.25rem;right:1.25rem;background:#1d1d1f;
           color:#fff;padding:.6rem 1rem;border-radius:8px;font-size:.8rem;
           opacity:0;transition:opacity .3s;pointer-events:none;max-width:340px;
           white-space:pre-wrap}
    .toast.show{opacity:.95}
    .kpi-trend{font-size:.7rem;margin-top:.35rem;font-weight:500}
    .trend-up{color:#34c759}.trend-down{color:#ff3b30}.trend-flat{color:#aeaeb2}
  </style>
</head>
<body>
<header>
  <div>
    <h1>Bright Smile Dental</h1>
    <div class="sub">Voice AI — Call Dashboard</div>
  </div>
  <span class="spacer"></span>
  <span id="refresh-ts" style="font-size:.72rem;color:#6e6e73;margin-right:.25rem"></span>
  <button class="btn btn-primary" onclick="loadAll()">Refresh</button>
  <button class="btn" id="sync-btn" onclick="syncRetell()">Sync History</button>
  <button class="btn" id="sync-ghl-btn" onclick="syncGhlAppts()" title="Pull GHL appointments into local DB and fix booking stats">Sync GHL Appts</button>
  <button class="btn" onclick="exportCsv()">Export CSV</button>
  <button class="btn" id="digest-btn" onclick="sendDigest()">Send Digest</button>
  <button class="btn" id="remind-btn" onclick="sendReminders()">Send Reminders</button>
  <button class="btn" onclick="openCpModal()" style="background:#f5f5f7;color:#1d1d1f">Change Password</button>
  <button class="btn" onclick="logout()">Logout</button>
</header>

<div class="wrap">
  <div class="kpi-grid" id="kpis"><div class="kpi"><div class="loading">Loading…</div></div></div>

  <div class="section" id="trend-section" style="display:none">
    <div class="section-hdr">Last 14 Days</div>
    <div class="chart-box">
      <div class="chart-legend">
        <span class="lg"><i style="background:#b7d7f7"></i>calls</span>
        <span class="lg"><i style="background:#34c759"></i>booked</span>
      </div>
      <div id="trend"></div>
    </div>
  </div>

  <div class="section" id="outcomes-section" style="display:none">
    <div class="section-hdr">Outcome Breakdown</div>
    <div class="outcome-grid" id="outcomes"></div>
  </div>

  <div class="section" id="np-section" style="display:none">
    <div class="section-hdr">New Patients per Month</div>
    <div class="chart-box"><div id="np-chart"></div></div>
  </div>

  <div class="tabs">
    <button class="tab active" id="tab-today" onclick="showTab('today')">Today</button>
    <button class="tab" id="tab-calls" onclick="showTab('calls')">Calls</button>
    <button class="tab" id="tab-appts" onclick="showTab('appts')">Upcoming Appointments</button>
    <button class="tab" id="tab-patients" onclick="showTab('patients');loadPatients()">Patients</button>
    <button class="tab" id="tab-waitlist" onclick="showTab('waitlist');loadWaitlist()">Waitlist <span id="wl-count"></span></button>
    <button class="tab" id="tab-analytics" onclick="showTab('analytics');loadAnalytics()">Analytics</button>
    <button class="tab" id="tab-issues" onclick="showTab('issues')">Alerts <span id="issue-count"></span></button>
    <button class="tab" id="tab-admin" onclick="showTab('admin');loadUsers()">Admin</button>
  </div>

  <div class="section" id="sec-today">
    <div class="section-hdr">Today's Overview</div>
    <div id="today-content"><div class="loading">Loading…</div></div>
  </div>

  <div class="section" id="sec-calls" style="display:none">
    <div class="section-hdr">Call Log
      <span class="filters">
        <select id="f-outcome" onchange="OFFSET=0;loadCalls()">
          <option value="">all outcomes</option>
          <option value="booked">booked</option>
          <option value="info_given">info given</option>
          <option value="abandoned">abandoned</option>
        </select>
        <input type="date" id="f-from" onchange="OFFSET=0;loadCalls()">
        <span>→</span>
        <input type="date" id="f-to" onchange="OFFSET=0;loadCalls()">
        <label><input type="checkbox" id="f-attn" onchange="renderCalls()"> ⚠ flagged only</label>
      </span>
    </div>
    <div id="calls"><div class="loading">Loading…</div></div>
    <div class="pager" id="pager" style="display:none">
      <span id="page-info"></span>
      <button class="btn" id="prev-btn" onclick="page(-1)">‹ Prev</button>
      <button class="btn" id="next-btn" onclick="page(1)">Next ›</button>
    </div>
  </div>

  <div class="section" id="sec-appts" style="display:none">
    <div class="section-hdr">Upcoming Appointments (booked by the voice agent)</div>
    <div id="appts"><div class="loading">Loading…</div></div>
  </div>

  <div class="section" id="sec-patients" style="display:none">
    <div class="section-hdr">Patient Registry
      <span class="filters">
        <input type="text" id="p-search" placeholder="search name or phone…"
               style="font-size:.75rem;padding:.3rem .5rem;border:1px solid #e5e5ea;border-radius:6px;width:200px"
               oninput="clearTimeout(window._pt);window._pt=setTimeout(loadPatients,300)">
      </span>
    </div>
    <div id="patients"><div class="loading">Loading…</div></div>
  </div>
  <div class="section" id="sec-patient-profile" style="display:none">
    <div class="section-hdr">Patient Profile
      <span class="filters"><button class="btn" onclick="closeProfile()">← back to list</button></span>
    </div>
    <div id="profile"></div>
  </div>

  <div class="section" id="sec-analytics" style="display:none">
    <div class="section-hdr">Call Analytics (last 30 days)
      <span class="filters">
        <button class="btn" onclick="viewReport('week')">Weekly Report</button>
        <button class="btn" onclick="viewReport('month')">Monthly Report</button>
        <button class="btn" id="report-btn" onclick="sendReport('week')">SMS Weekly Report</button>
      </span>
    </div>
    <div id="analytics"><div class="loading">Loading…</div></div>
  </div>

  <div class="section" id="sec-waitlist" style="display:none">
    <div class="section-hdr">Waitlist — auto-offered when a matching slot frees up</div>
    <div id="waitlist"><div class="loading">Loading…</div></div>
  </div>

  <div class="section" id="sec-admin" style="display:none">
    <div class="section-hdr">Dashboard Users (admin)
      <span class="filters">
        <input type="text" id="nu-user" placeholder="username" style="width:110px;font-size:.75rem;padding:.3rem .5rem;border:1px solid #e5e5ea;border-radius:6px">
        <input type="password" id="nu-pass" placeholder="password (8+)" style="width:120px;font-size:.75rem;padding:.3rem .5rem;border:1px solid #e5e5ea;border-radius:6px">
        <select id="nu-role"><option value="staff">staff</option><option value="admin">admin</option></select>
        <button class="btn" onclick="createUser()">Add user</button>
      </span>
    </div>
    <div id="users"><div class="loading">Loading…</div></div>
  </div>

  <!-- Change Password modal -->
  <div id="cpModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:center;justify-content:center">
    <div style="background:#fff;border-radius:14px;padding:1.8rem;width:320px;box-shadow:0 8px 32px rgba(0,0,0,.18)">
      <h3 style="font-size:.95rem;margin-bottom:1.1rem">Change Password</h3>
      <label style="font-size:.7rem;color:#6e6e73;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:.25rem">Current password</label>
      <input type="password" id="cp-cur" style="width:100%;padding:.55rem .7rem;border:1px solid #e5e5ea;border-radius:8px;font-size:.9rem;margin-bottom:.8rem">
      <label style="font-size:.7rem;color:#6e6e73;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:.25rem">New password (8+ characters)</label>
      <input type="password" id="cp-new" style="width:100%;padding:.55rem .7rem;border:1px solid #e5e5ea;border-radius:8px;font-size:.9rem;margin-bottom:.8rem">
      <label style="font-size:.7rem;color:#6e6e73;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:.25rem">Confirm new password</label>
      <input type="password" id="cp-cnf" style="width:100%;padding:.55rem .7rem;border:1px solid #e5e5ea;border-radius:8px;font-size:.9rem;margin-bottom:1rem">
      <div id="cp-err" style="color:#c0392b;font-size:.8rem;min-height:1rem;margin-bottom:.7rem"></div>
      <div style="display:flex;gap:.6rem">
        <button class="btn" onclick="submitChangePassword()" style="flex:1">Save</button>
        <button class="btn" onclick="closeCpModal()" style="flex:1;background:#f5f5f7;color:#1d1d1f">Cancel</button>
      </div>
    </div>
  </div>

  <div class="section" id="sec-issues" style="display:none">
    <div class="section-hdr">Failed Webhook Events (dead-letter queue)</div>
    <div id="issues"><div class="loading">Loading…</div></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const LIMIT = 50;
let OFFSET = 0, TOTAL = 0, CALL_ROWS = [];

function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function dur(s){if(!s)return'—';const m=Math.floor(s/60),r=s%60;return m?`${m}m ${r}s`:`${r}s`}
function ts(iso){if(!iso)return'—';return new Date(iso).toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
function badge(o){const m={booked:'booked',info_given:'info_given',abandoned:'abandoned'};const c=m[o]||'other';return`<span class="badge b-${c}">${esc((o||'unknown').replace('_',' '))}</span>`}
function dotcls(o){const m={booked:'booked',info_given:'info_given',abandoned:'abandoned'};return m[o]||'other'}
function toggle(id){const e=document.getElementById(id);e.style.display=e.style.display==='block'?'none':'block'}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),4000)}
function showError(id,msg){document.getElementById(id).innerHTML='<div class="empty">'+esc(msg)+'</div>'}

async function getJSON(url){
  const r = await fetch(url, {credentials:'same-origin'});
  if(r.status === 401){window.location='/login';throw new Error('Session expired — redirecting to login');}
  if(!r.ok) throw new Error('Request failed (HTTP ' + r.status + ')');
  return r.json();
}
function tok(extra){return new URLSearchParams(extra||{}).toString()}
function filterParams(){
  const p={};
  const o=document.getElementById('f-outcome').value; if(o)p.outcome=o;
  const f=document.getElementById('f-from').value; if(f)p.from=f;
  const t=document.getElementById('f-to').value; if(t)p.to=t;
  return p;
}

function showTab(name){
  for(const t of ['today','calls','appts','patients','waitlist','analytics','issues','admin']){
    document.getElementById('sec-'+t).style.display = t===name?'':'none';
    document.getElementById('tab-'+t).classList.toggle('active', t===name);
  }
  document.getElementById('sec-patient-profile').style.display='none';
  if(name==='patients')document.getElementById('sec-patients').style.display='';
  if(name==='today')loadToday();
}
async function logout(){
  try{await fetch('/api/logout',{method:'POST',credentials:'same-origin'})}catch(e){}
  window.location='/login';
}
function closeProfile(){
  document.getElementById('sec-patient-profile').style.display='none';
  document.getElementById('sec-patients').style.display='';
}

// ---- KPIs + trend ----
async function loadKpis(){
  try{
    const d=await getJSON('/api/kpis?'+tok());
    const rate=d.booking_rate_pct!=null?d.booking_rate_pct+'%':'—';
    const tr=d.daily_trend||[];
    const tw=tr.slice(-7),pw=tr.slice(-14,-7);
    const twC=tw.reduce((s,x)=>s+x.calls,0),pwC=pw.reduce((s,x)=>s+x.calls,0);
    const twB=tw.reduce((s,x)=>s+x.booked,0),pwB=pw.reduce((s,x)=>s+x.booked,0);
    function trendBadge(cur,prev){
      if(!prev||!cur)return'';
      const delta=cur-prev,pct=Math.round(Math.abs(delta)/prev*100);
      if(pct<5)return`<div class="kpi-trend trend-flat">→ flat vs last wk</div>`;
      return delta>0
        ?`<div class="kpi-trend trend-up">↑${pct}% vs last wk</div>`
        :`<div class="kpi-trend trend-down">↓${pct}% vs last wk</div>`;
    }
    document.getElementById('kpis').innerHTML=`
      <div class="kpi"><div class="kpi-val">${esc(d.calls_today)}</div><div class="kpi-lbl">Calls Today</div></div>
      <div class="kpi"><div class="kpi-val">${esc(d.calls_this_week)}</div><div class="kpi-lbl">This Week</div>${trendBadge(twC,pwC)}</div>
      <div class="kpi"><div class="kpi-val">${esc(d.total_calls)}</div><div class="kpi-lbl">All Time</div></div>
      <div class="kpi"><div class="kpi-val">${rate}</div><div class="kpi-lbl">Booking Rate</div>${trendBadge(twB,pwB)}</div>
      <div class="kpi"><div class="kpi-val">${dur(d.avg_duration_seconds)}</div><div class="kpi-lbl">Avg Duration</div></div>
      <div class="kpi"><div class="kpi-val" style="color:#5b21b6">${esc(d.new_patients_this_month??'—')}</div><div class="kpi-lbl">New Patients (Month)</div></div>
      <div class="kpi"><div class="kpi-val" style="color:#065f46">$${(d.revenue_this_month??0).toLocaleString()}</div><div class="kpi-lbl">Est. Revenue (Month)</div></div>
    `;
    const ob=d.outcome_breakdown||{};
    if(Object.keys(ob).length){
      let html='';
      for(const[k,v]of Object.entries(ob).sort((a,b)=>b[1]-a[1])){
        html+=`<div class="ob"><div class="ob-dot dot-${dotcls(k)}"></div><span class="ob-name">${esc(k.replace('_',' '))}</span><span class="ob-n">${v}</span></div>`;
      }
      document.getElementById('outcomes').innerHTML=html;
      document.getElementById('outcomes-section').style.display='block';
    }
    renderTrend(d.daily_trend||[]);
    renderNewPatients(d.monthly_new_patients||[]);
  }catch(e){console.error('kpi',e);showError('kpis',e.message)}
}

function renderNewPatients(monthly){
  if(!monthly.length||!monthly.some(m=>m.new_patients>0))return;
  const W=1100,H=110,PAD=4,n=monthly.length;
  const max=Math.max(1,...monthly.map(m=>m.new_patients));
  const bw=(W-PAD*2)/n;
  let bars='';
  monthly.forEach((m,i)=>{
    const x=PAD+i*bw, bh=m.new_patients/max*(H-28);
    bars+=`<g><title>${esc(m.month)}: ${m.new_patients} new patients</title>
      <rect x="${(x+bw*0.25).toFixed(1)}" y="${(H-bh-14).toFixed(1)}" width="${(bw*0.5).toFixed(1)}" height="${bh.toFixed(1)}" rx="3" fill="#8b5cf6"/>
      <text x="${(x+bw/2).toFixed(1)}" y="${(H-bh-18).toFixed(1)}" font-size="11" font-weight="600" fill="#5b21b6" text-anchor="middle">${m.new_patients||''}</text>
      <text x="${(x+bw/2).toFixed(1)}" y="${H}" font-size="9" fill="#aeaeb2" text-anchor="middle">${esc(m.month)}</text></g>`;
  });
  document.getElementById('np-chart').innerHTML=
    `<svg viewBox="0 0 ${W} ${H+4}" style="width:100%;height:auto" xmlns="http://www.w3.org/2000/svg">${bars}</svg>`;
  document.getElementById('np-section').style.display='block';
}

function renderTrend(trend){
  if(!trend.length)return;
  const W=1100,H=120,PAD=4,n=trend.length;
  const max=Math.max(1,...trend.map(d=>d.calls));
  const bw=(W-PAD*2)/n;
  let bars='';
  trend.forEach((d,i)=>{
    const x=PAD+i*bw;
    const ch=d.calls/max*(H-20), bh=d.booked/max*(H-20);
    const lbl=`${d.date}: ${d.calls} calls, ${d.booked} booked`;
    bars+=`<g><title>${esc(lbl)}</title>
      <rect x="${(x+bw*0.15).toFixed(1)}" y="${(H-ch).toFixed(1)}" width="${(bw*0.7).toFixed(1)}" height="${ch.toFixed(1)}" rx="2" fill="#b7d7f7"/>
      <rect x="${(x+bw*0.15).toFixed(1)}" y="${(H-bh).toFixed(1)}" width="${(bw*0.7).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="#34c759"/>
      <text x="${(x+bw/2).toFixed(1)}" y="${H+12}" font-size="8" fill="#aeaeb2" text-anchor="middle">${esc(d.date.slice(5))}</text></g>`;
  });
  document.getElementById('trend').innerHTML=
    `<svg viewBox="0 0 ${W} ${H+18}" style="width:100%;height:auto" xmlns="http://www.w3.org/2000/svg">${bars}</svg>`;
  document.getElementById('trend-section').style.display='block';
}

// ---- Call log ----
async function loadCalls(){
  try{
    const d=await getJSON('/api/calls?'+tok({...filterParams(),offset:OFFSET,limit:LIMIT}));
    CALL_ROWS=d.rows; TOTAL=d.total;
    renderCalls();
  }catch(e){showError('calls',e.message||'Failed to load.')}
}

function renderCalls(){
  const attnOnly=document.getElementById('f-attn').checked;
  const rows=attnOnly?CALL_ROWS.filter(r=>r.attention):CALL_ROWS;
  if(!rows.length){
    document.getElementById('calls').innerHTML='<div class="empty">'+(attnOnly?'No flagged calls on this page.':'No calls found.')+'</div>';
  }else{
    let t=`<table><thead><tr><th>Time</th><th>Phone</th><th>Duration</th><th>Outcome</th><th>Summary</th><th>Recording</th></tr></thead><tbody>`;
    rows.forEach((r,i)=>{
      const hasTx=r.transcript&&r.transcript.trim();
      const hasRec=r.recording_ref&&r.recording_ref.trim();
      const attn=r.attention?`<span class="badge b-attn" title="${esc(r.attention_reasons.join(', '))}">⚠ attention</span><div class="attn-why">${esc(r.attention_reasons.join(' · '))}</div>`:'';
      const newB=r.new_caller?` <span class="badge b-new" title="first call from this number">NEW</span>`:'';
      const unv=r.booking_verified===false?` <span class="badge b-unverified" title="call marked booked but no active GHL appointment found">unverified</span>`:'';
      t+=`<tr${r.new_caller?' class="row-new"':''}>
        <td style="white-space:nowrap">${ts(r.ended_at)}</td>
        <td style="white-space:nowrap">${esc(r.phone)}${newB}</td>
        <td style="white-space:nowrap">${dur(r.duration_seconds)}</td>
        <td>${badge(r.outcome)}${unv}${attn}</td>
        <td class="summary-wrap">${r.summary?esc(r.summary):'<span class="no-sum">no summary</span>'}${hasTx?`<button class="tx-btn" onclick="toggle('tx${i}')">transcript ▾</button><div class="tx-box" id="tx${i}">${esc(r.transcript)}</div>`:''}</td>
        <td style="white-space:nowrap">${hasRec?`<button class="tx-btn" onclick="toggle('rec${i}')">▶ play</button><div class="tx-box" id="rec${i}" style="min-width:220px"><audio controls src="/api/recording/${encodeURIComponent(r.call_id)}?${tok()}" style="width:100%;margin-top:.2rem" preload="none"></audio></div>`:'<span class="no-sum">—</span>'}</td>
      </tr>`;
    });
    t+='</tbody></table>';
    document.getElementById('calls').innerHTML=t;
  }
  const pager=document.getElementById('pager');
  pager.style.display=TOTAL>LIMIT?'flex':'none';
  document.getElementById('page-info').textContent=
    `${TOTAL?OFFSET+1:0}–${Math.min(OFFSET+LIMIT,TOTAL)} of ${TOTAL}`;
  document.getElementById('prev-btn').disabled=OFFSET<=0;
  document.getElementById('next-btn').disabled=OFFSET+LIMIT>=TOTAL;
}

function page(dir){
  OFFSET=Math.max(0,OFFSET+dir*LIMIT);
  loadCalls();
}

// ---- Appointments ----
async function loadAppts(){
  try{
    const rows=await getJSON('/api/appointments?'+tok());
    if(!rows.length){document.getElementById('appts').innerHTML='<div class="empty">No upcoming appointments.</div>';return}
    let t=`<table><thead><tr><th>When</th><th>Patient</th><th>Phone</th><th>Service</th><th>Insurance</th><th>Status</th><th>Reminder</th><th>GHL ID</th></tr></thead><tbody>`;
    for(const a of rows){
      const when=a.start_local?new Date(a.start_local).toLocaleString('en-US',{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'—';
      const tag=a.is_new_patient?`<span class="badge b-new" title="first appointment for this phone number">NEW PATIENT</span>`:`<span class="badge b-returning">returning</span>`;
      t+=`<tr${a.is_new_patient?' class="row-new"':''}>
        <td style="white-space:nowrap">${esc(when)}</td>
        <td style="white-space:nowrap">${esc(a.name)} ${tag}</td>
        <td>${esc(a.phone)}</td>
        <td>${esc(a.service||'—')}</td>
        <td>${a.insurance?esc(a.insurance):'<span class="no-sum">—</span>'}</td>
        <td><span class="badge b-${a.status==='confirmed'?'confirmed':'other'}">${esc(a.status||'—')}</span></td>
        <td>${a.reminder_sent?'✓ sent':'<span class="no-sum">—</span>'}</td>
        <td style="font-size:.72rem;color:#6e6e73">${esc(a.ghl_appointment_id||'—')}</td>
      </tr>`;
    }
    document.getElementById('appts').innerHTML=t+'</tbody></table>';
  }catch(e){showError('appts',e.message)}
}

// ---- Failed events ----
async function loadIssues(){
  try{
    const rows=await getJSON('/api/failed-events?'+tok());
    document.getElementById('issue-count').textContent=rows.length?`(${rows.length})`:'';
    if(!rows.length){document.getElementById('issues').innerHTML='<div class="empty">No failed events — every webhook persisted cleanly.</div>';return}
    let t=`<table><thead><tr><th>When</th><th>Call ID</th><th>Error</th><th></th></tr></thead><tbody>`;
    for(const e of rows){
      t+=`<tr>
        <td style="white-space:nowrap">${ts(e.created_at)}</td>
        <td style="font-size:.72rem">${esc(e.call_id||'—')}</td>
        <td style="font-size:.75rem;color:#991b1b;max-width:420px">${esc(e.error)}</td>
        <td><button class="btn" onclick="replayEvent(${e.id},this)">Replay</button></td>
      </tr>`;
    }
    document.getElementById('issues').innerHTML=t+'</tbody></table>';
  }catch(e){showError('issues',e.message)}
}

async function replayEvent(id,btn){
  btn.disabled=true;btn.textContent='Replaying…';
  try{
    const r=await fetch(`/api/failed-events/${id}/replay?`+tok(),{method:'POST'});
    const d=await r.json();
    if(d.ok){toast('Replayed OK — call '+(d.call_id||''));loadIssues();loadCalls()}
    else{toast('Replay failed: '+(d.error||'unknown'));btn.disabled=false;btn.textContent='Replay'}
  }catch(e){toast('Replay failed: '+e.message);btn.disabled=false;btn.textContent='Replay'}
}

// ---- Header actions ----
async function syncRetell(){
  const btn=document.getElementById('sync-btn');
  btn.disabled=true;btn.textContent='Syncing…';
  try{
    const r=await fetch('/api/sync-retell?'+tok(),{method:'POST'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    btn.textContent=`Done — ${d.synced} synced`;
    setTimeout(()=>{btn.textContent='Sync History';btn.disabled=false},4000);
    loadAll();
  }catch(e){
    btn.textContent='Failed';
    setTimeout(()=>{btn.textContent='Sync History';btn.disabled=false},3000);
  }
}

async function syncGhlAppts(){
  const btn=document.getElementById('sync-ghl-btn');
  btn.disabled=true;btn.textContent='Syncing GHL…';
  try{
    const r=await fetch('/api/sync-ghl-appointments?'+tok(),{method:'POST'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.error){throw new Error(d.error)}
    btn.textContent=`Done — ${d.appointments_created} created, ${d.calls_linked} calls fixed`;
    setTimeout(()=>{btn.textContent='Sync GHL Appts';btn.disabled=false},6000);
    loadAll();
  }catch(e){
    toast('GHL sync failed: '+e.message);
    btn.textContent='Sync GHL Appts';btn.disabled=false;
  }
}

function exportCsv(){
  window.location='/api/calls.csv?'+tok(filterParams());
}

async function sendDigest(){
  const btn=document.getElementById('digest-btn');
  btn.disabled=true;
  try{
    const prev=await getJSON('/api/digest?'+tok());
    if(!confirm('Send this digest?\\n\\n'+prev.text)){btn.disabled=false;return}
    const r=await fetch('/api/send-digest?'+tok(),{method:'POST'});
    const d=await r.json();
    toast(d.sent?('Digest sent to '+d.to):('Not sent: '+d.reason));
  }catch(e){toast('Digest failed: '+e.message)}
  btn.disabled=false;
}

// ---- Patients ----
async function loadPatients(){
  try{
    const q=document.getElementById('p-search').value;
    const d=await getJSON('/api/patients?'+tok({search:q,limit:100}));
    if(!d.rows.length){document.getElementById('patients').innerHTML='<div class="empty">No patients found.</div>';return}
    let t=`<table><thead><tr><th>Patient</th><th>Phone</th><th>Insurance</th><th>Calls</th><th>Appointments</th><th>First Seen</th><th></th></tr></thead><tbody>`;
    for(const p of d.rows){
      t+=`<tr>
        <td>${esc(p.name)}</td>
        <td>${esc(p.phone_masked)}</td>
        <td>${p.insurance?esc(p.insurance):'<span class="no-sum">—</span>'}</td>
        <td>${p.calls}</td><td>${p.appointments}</td>
        <td style="white-space:nowrap">${ts(p.first_seen_at)}</td>
        <td><button class="btn" onclick="openProfile(${p.id})">View</button></td>
      </tr>`;
    }
    document.getElementById('patients').innerHTML=t+'</tbody></table>';
  }catch(e){showError('patients',e.message)}
}

async function openProfile(id){
  try{
    const p=await getJSON(`/api/patients/${id}?`+tok());
    const st=p.stats||{};
    let h=`<div style="padding:1rem 1.5rem">
      <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:1rem;align-items:flex-end">
        <div><div class="kpi-lbl">Name</div><input id="pf-name" value="${esc(p.name==='(name unknown)'?'':p.name)}" style="font-weight:600;font-size:1rem;border:1px solid #e5e5ea;border-radius:6px;padding:.3rem .5rem"></div>
        <div><div class="kpi-lbl">Phone</div><div style="font-weight:600;padding:.3rem 0">${esc(p.phone||'—')}</div></div>
        <div><div class="kpi-lbl">Insurance</div><input id="pf-ins" value="${esc(p.insurance||'')}" style="border:1px solid #e5e5ea;border-radius:6px;padding:.3rem .5rem;width:130px"></div>
        <div><div class="kpi-lbl">DOB</div><input id="pf-dob" value="${esc(p.dob||'')}" placeholder="YYYY-MM-DD" style="border:1px solid #e5e5ea;border-radius:6px;padding:.3rem .5rem;width:110px"></div>
        <button class="btn btn-primary" onclick="savePatient(${p.id})">Save</button>
        <button class="btn" onclick="callPatient(${p.id})">📞 Call patient</button>
      </div>
      <div style="display:flex;gap:.6rem;flex-wrap:wrap;margin-bottom:1rem">
        <span class="badge b-info_given">${st.total_calls??0} calls</span>
        <span class="badge b-booked">${st.visits??0} visits</span>
        <span class="badge b-abandoned">${st.no_shows??0} no-shows</span>
        <span class="badge b-new">${st.upcoming??0} upcoming</span>
        <span class="badge b-other">first seen ${ts(p.first_seen_at)}</span>
      </div>
      <div class="kpi-lbl" style="margin-bottom:.3rem">Staff Notes</div>
      <textarea id="p-notes" style="width:100%;min-height:60px;border:1px solid #e5e5ea;border-radius:8px;padding:.6rem;font:inherit;font-size:.85rem">${esc(p.notes||'')}</textarea>
      <button class="btn" style="margin-top:.4rem" onclick="saveNotes(${p.id})">Save notes</button>
      <span style="margin-left:1.2rem;font-size:.75rem;color:#6e6e73">Same person, second number?
        merge patient #<input id="pf-merge" style="width:52px;border:1px solid #e5e5ea;border-radius:6px;padding:.15rem .3rem"> into this one
        <button class="btn" onclick="mergePatient(${p.id})">Merge</button></span>`;
    if(p.waitlist&&p.waitlist.length){
      h+=`<div class="kpi-lbl" style="margin:1.2rem 0 .4rem">Waitlist Entries</div>`;
      for(const w of p.waitlist)h+=`<div style="font-size:.8rem">• ${esc(w.service||'—')} — ${esc(w.preferred_day||'any day')}${w.time_note?', '+esc(w.time_note):''} <span class="badge b-${w.status==='waiting'?'info_given':'other'}">${esc(w.status)}</span></div>`;
    }
    h+=`<div class="kpi-lbl" style="margin:1.2rem 0 .4rem">Appointments (${p.appointments.length})</div>`;
    if(p.appointments.length){
      h+='<table><thead><tr><th>When</th><th>Service</th><th>Status</th><th>Reason</th><th>Reminder</th></tr></thead><tbody>';
      for(const a of p.appointments){
        h+=`<tr><td style="white-space:nowrap">${ts(a.start_utc)}</td><td>${esc(a.service||'—')}</td>
          <td><span class="badge b-${a.status==='confirmed'?'confirmed':(a.status==='noshow'?'abandoned':'other')}">${esc(a.status||'—')}</span></td>
          <td>${esc(a.reason||'—')}</td><td>${a.reminder_sent?'✓':'—'}</td></tr>`;
      }
      h+='</tbody></table>';
    }else h+='<div class="no-sum">none</div>';
    h+=`<div class="kpi-lbl" style="margin:1.2rem 0 .4rem">Calls (${p.calls.length})</div>`;
    if(p.calls.length){
      h+='<table><thead><tr><th>Time</th><th>Outcome</th><th>Duration</th><th>Summary</th></tr></thead><tbody>';
      for(const c of p.calls){
        h+=`<tr><td style="white-space:nowrap">${ts(c.ended_at)}</td><td>${badge(c.outcome)}</td>
          <td>${dur(c.duration_seconds)}</td><td class="summary-wrap">${c.summary?esc(c.summary):'<span class="no-sum">—</span>'}</td></tr>`;
      }
      h+='</tbody></table>';
    }else h+='<div class="no-sum">none</div>';
    h+='</div>';
    document.getElementById('profile').innerHTML=h;
    document.getElementById('sec-patients').style.display='none';
    document.getElementById('sec-patient-profile').style.display='';
  }catch(e){toast('Profile failed: '+e.message)}
}

async function saveNotes(id){
  try{
    const r=await fetch(`/api/patients/${id}/notes?`+tok(),{method:'PUT',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({notes:document.getElementById('p-notes').value})});
    if(!r.ok)throw new Error('HTTP '+r.status);
    toast('Notes saved');
  }catch(e){toast('Save failed: '+e.message)}
}

async function savePatient(id){
  try{
    const r=await fetch(`/api/patients/${id}?`+tok(),{method:'PUT',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:document.getElementById('pf-name').value,
                           insurance:document.getElementById('pf-ins').value,
                           dob:document.getElementById('pf-dob').value})});
    if(!r.ok)throw new Error('HTTP '+r.status);
    toast('Patient updated');loadPatients();
  }catch(e){toast('Save failed: '+e.message)}
}

async function callPatient(id){
  if(!confirm('Sarah will call this patient now. Continue?'))return;
  try{
    const r=await fetch(`/api/call-patient/${id}?`+tok(),{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
    const d=await r.json();
    toast(d.ok?'Call placed — Sarah is dialing':'Call failed: '+(d.error||r.status));
  }catch(e){toast('Call failed: '+e.message)}
}

async function mergePatient(target){
  const src=parseInt(document.getElementById('pf-merge').value,10);
  if(!src){toast('Enter the patient # to merge');return}
  if(!confirm(`Merge patient #${src} INTO this patient? Their calls and appointments move here and #${src} is deleted.`))return;
  try{
    const r=await fetch(`/api/patients/${target}/merge/${src}?`+tok(),{method:'POST'});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));
    toast(`Merged — ${d.calls_moved} calls, ${d.appointments_moved} appointments moved`);
    openProfile(target);loadPatients();
  }catch(e){toast('Merge failed: '+e.message)}
}

// ---- Waitlist ----
async function loadWaitlist(){
  try{
    const rows=await getJSON('/api/waitlist?'+tok());
    document.getElementById('wl-count').textContent=rows.filter(w=>w.status==='waiting').length||'';
    if(!rows.length){document.getElementById('waitlist').innerHTML='<div class="empty">Waitlist is empty. Sarah offers it when a requested day is full.</div>';return}
    let t=`<table><thead><tr><th>Added</th><th>Name</th><th>Phone</th><th>Service</th><th>Wants</th><th>Status</th><th></th></tr></thead><tbody>`;
    for(const w of rows){
      t+=`<tr>
        <td style="white-space:nowrap">${ts(w.created_at)}</td>
        <td>${esc(w.name)}</td><td>${esc(w.phone_masked)}</td>
        <td>${esc(w.service||'—')}</td>
        <td>${esc(w.preferred_day||'any day')}${w.time_note?' · '+esc(w.time_note):''}</td>
        <td><span class="badge b-${w.status==='waiting'?'info_given':(w.status==='offered'?'booked':'other')}">${esc(w.status)}</span>${w.offered_detail?`<div class="attn-why" style="color:#6e6e73">${esc(w.offered_detail)}</div>`:''}</td>
        <td style="white-space:nowrap">
          <button class="btn" onclick="offerWaitlist(${w.id},this)">Offer (SMS)</button>
          <button class="btn" onclick="removeWaitlist(${w.id})">Remove</button>
        </td></tr>`;
    }
    document.getElementById('waitlist').innerHTML=t+'</tbody></table>';
  }catch(e){showError('waitlist',e.message)}
}
async function offerWaitlist(id,btn){
  btn.disabled=true;
  try{
    const r=await fetch(`/api/waitlist/${id}/offer?`+tok(),{method:'POST'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    toast('Offer SMS sent');loadWaitlist();
  }catch(e){toast('Offer failed: '+e.message);btn.disabled=false}
}
async function removeWaitlist(id){
  try{
    const r=await fetch(`/api/waitlist/${id}/remove?`+tok(),{method:'POST'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    loadWaitlist();
  }catch(e){toast('Remove failed: '+e.message)}
}

// ---- Admin users ----
async function loadUsers(){
  try{
    const rows=await getJSON('/api/users');
    if(!rows.length){document.getElementById('users').innerHTML='<div class="empty">No users yet — add one above, or set DASHBOARD_ADMIN_USER / DASHBOARD_ADMIN_PASSWORD env vars for the first admin.</div>';return}
    let t=`<table><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Last Login</th><th>Actions</th></tr></thead><tbody>`;
    for(const u of rows){
      const statusBadge=u.active?'<span class="badge b-booked">active</span>':'<span class="badge b-other">disabled</span>';
      const roleOpts=['admin','staff'].map(r=>`<option value="${r}"${r===u.role?' selected':''}>${r}</option>`).join('');
      const actions=u.active
        ?`<button class="btn" style="font-size:.7rem;padding:.25rem .55rem" onclick="disableUser(${u.id})">Disable</button>
           <button class="btn" style="font-size:.7rem;padding:.25rem .55rem;background:#f5f5f7;color:#1d1d1f" onclick="promptResetPassword(${u.id})">Reset pwd</button>
           <select onchange="changeRole(${u.id},this.value)" style="font-size:.7rem;padding:.25rem .4rem;border:1px solid #e5e5ea;border-radius:6px">${roleOpts}</select>`
        :`<button class="btn" style="font-size:.7rem;padding:.25rem .55rem;background:#34c759;color:#fff" onclick="enableUser(${u.id})">Enable</button>
           <button class="btn" style="font-size:.7rem;padding:.25rem .55rem;background:#f5f5f7;color:#1d1d1f" onclick="promptResetPassword(${u.id})">Reset pwd</button>`;
      t+=`<tr><td>${esc(u.username)}</td><td>${esc(u.role)}</td><td>${statusBadge}</td>
        <td style="white-space:nowrap">${ts(u.last_login_at)}</td><td style="white-space:nowrap;display:flex;gap:.35rem;align-items:center">${actions}</td></tr>`;
    }
    document.getElementById('users').innerHTML=t+'</tbody></table>';
  }catch(e){
    document.getElementById('users').innerHTML='<div class="empty">'+esc(e.message.includes('403')?'Admin access required.':e.message)+'</div>';
  }
}
async function createUser(){
  try{
    const r=await fetch('/api/users',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:document.getElementById('nu-user').value,
                           password:document.getElementById('nu-pass').value,
                           role:document.getElementById('nu-role').value})});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));
    toast('User created: '+d.username);
    document.getElementById('nu-user').value='';document.getElementById('nu-pass').value='';
    loadUsers();
  }catch(e){toast('Create failed: '+e.message)}
}
async function disableUser(id){
  if(!confirm('Disable this user?'))return;
  try{
    const r=await fetch(`/api/users/${id}/disable`,{method:'POST',credentials:'same-origin'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    toast('User disabled');loadUsers();
  }catch(e){toast('Disable failed: '+e.message)}
}
async function enableUser(id){
  try{
    const r=await fetch(`/api/users/${id}/enable`,{method:'POST',credentials:'same-origin'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    toast('User re-enabled');loadUsers();
  }catch(e){toast('Enable failed: '+e.message)}
}
async function promptResetPassword(id){
  const pwd=prompt('Set new password for this user (8+ characters):');
  if(!pwd)return;
  try{
    const r=await fetch(`/api/users/${id}/password`,{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pwd})});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));
    toast('Password reset — user must log in again');
  }catch(e){toast('Reset failed: '+e.message)}
}
async function changeRole(id,role){
  try{
    const r=await fetch(`/api/users/${id}/role`,{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({role})});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));
    toast('Role updated to '+role);loadUsers();
  }catch(e){toast('Role change failed: '+e.message);loadUsers();}
}
// ---- Change Password modal ----
function openCpModal(){
  document.getElementById('cp-cur').value='';
  document.getElementById('cp-new').value='';
  document.getElementById('cp-cnf').value='';
  document.getElementById('cp-err').textContent='';
  const m=document.getElementById('cpModal');
  m.style.display='flex';
}
function closeCpModal(){document.getElementById('cpModal').style.display='none';}
async function submitChangePassword(){
  const cur=document.getElementById('cp-cur').value;
  const nw=document.getElementById('cp-new').value;
  const cnf=document.getElementById('cp-cnf').value;
  const errEl=document.getElementById('cp-err');
  if(nw!==cnf){errEl.textContent='New passwords do not match';return;}
  if(nw.length<8){errEl.textContent='New password must be at least 8 characters';return;}
  errEl.textContent='';
  try{
    const r=await fetch('/api/me/password',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({current_password:cur,new_password:nw})});
    const d=await r.json();
    if(!r.ok){errEl.textContent=d.detail||('Error '+r.status);return;}
    toast('Password changed — please log in again');
    closeCpModal();
    setTimeout(()=>window.location='/login',1500);
  }catch(e){errEl.textContent=e.message;}
}

// ---- Analytics ----
function barChart(values,labels,color,h=100){
  const W=1100,PAD=4,n=values.length,max=Math.max(1,...values);
  const bw=(W-PAD*2)/n;let bars='';
  values.forEach((v,i)=>{
    const x=PAD+i*bw,bh=v/max*(h-26);
    bars+=`<g><title>${esc(labels[i])}: ${v}</title>
      <rect x="${(x+bw*0.18).toFixed(1)}" y="${(h-bh-14).toFixed(1)}" width="${(bw*0.64).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="${color}"/>
      ${v?`<text x="${(x+bw/2).toFixed(1)}" y="${(h-bh-17).toFixed(1)}" font-size="9" fill="#6e6e73" text-anchor="middle">${v}</text>`:''}
      <text x="${(x+bw/2).toFixed(1)}" y="${h-2}" font-size="8" fill="#aeaeb2" text-anchor="middle">${esc(labels[i])}</text></g>`;
  });
  return `<svg viewBox="0 0 ${W} ${h}" style="width:100%;height:auto" xmlns="http://www.w3.org/2000/svg">${bars}</svg>`;
}

async function loadAnalytics(){
  try{
    const a=await getJSON('/api/analytics?'+tok());
    const days=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    const hours=[...Array(24).keys()].map(h=>h===0?'12a':h<12?h+'a':h===12?'12p':(h-12)+'p');
    const rev=a.revenue,ns=a.no_show;
    let h=`<div style="padding:1rem 1.5rem">
      <div class="kpi-grid" style="margin-bottom:1rem">
        <div class="kpi"><div class="kpi-val">${a.conversion_pct}%</div><div class="kpi-lbl">Conversion (30d)</div></div>
        <div class="kpi"><div class="kpi-val">${a.abandon_pct}%</div><div class="kpi-lbl">Abandon Rate</div></div>
        <div class="kpi"><div class="kpi-val">${a.after_hours_calls}</div><div class="kpi-lbl">After-Hours Calls</div></div>
        <div class="kpi"><div class="kpi-val">${a.faq_fallbacks}</div><div class="kpi-lbl">Unanswered Questions</div></div>
        <div class="kpi"><div class="kpi-val">${ns.no_show_rate_pct}%</div><div class="kpi-lbl">No-Show Rate</div></div>
      </div>
      <div class="kpi-lbl" style="margin-bottom:.4rem">Calls by Hour (local)</div>
      ${barChart(a.hourly_distribution,hours,'#b7d7f7',96)}
      <div class="kpi-lbl" style="margin:1rem 0 .4rem">Calls by Weekday (green = booked)</div>
      ${barChart(a.weekday_distribution,days,'#b7d7f7',96)}
      ${barChart(a.weekday_booked,days,'#34c759',72)}
      <div class="kpi-lbl" style="margin:1rem 0 .4rem">Estimated Revenue (all time, booked × price)</div>
      <table><thead><tr><th>Service</th><th>Bookings</th><th>Est. Revenue</th></tr></thead><tbody>`;
    for(const s of rev.by_service){
      h+=`<tr><td>${esc(s.service)}</td><td>${s.count}</td><td>$${s.revenue.toLocaleString()}</td></tr>`;
    }
    h+=`</tbody></table>
      <div class="no-sum" style="margin-top:.3rem;font-size:.72rem">${esc(rev.note)}</div>
      <div class="kpi-lbl" style="margin:1rem 0 .4rem">No-Shows vs Reminders</div>
      <div>With reminder: ${ns.with_reminder.no_shows}/${ns.with_reminder.total} (${ns.with_reminder.rate_pct}%) ·
           Without: ${ns.without_reminder.no_shows}/${ns.without_reminder.total} (${ns.without_reminder.rate_pct}%)</div>`;
    if(a.unanswered_samples.length){
      h+=`<div class="kpi-lbl" style="margin:1rem 0 .4rem">Sample Unanswered Calls (consider adding to FAQ)</div>`;
      for(const s of a.unanswered_samples)h+=`<div style="font-size:.8rem;color:#3a3a3c;margin:.2rem 0">• ${esc(s)}</div>`;
    }
    h+=`<div class="tx-box" id="report-box" style="margin-top:1rem"></div></div>`;
    document.getElementById('analytics').innerHTML=h;
  }catch(e){showError('analytics',e.message)}
}

async function viewReport(period){
  try{
    const r=await getJSON('/api/report?'+tok({period}));
    const box=document.getElementById('report-box');
    box.textContent=r.text;box.style.display='block';
    box.scrollIntoView({behavior:'smooth'});
  }catch(e){toast('Report failed: '+e.message)}
}

async function sendReport(period){
  const btn=document.getElementById('report-btn');btn.disabled=true;
  try{
    const r=await fetch('/api/send-report?'+tok({period}),{method:'POST'});
    const d=await r.json();
    toast(d.sent?('Report sent to '+d.to):('Not sent: '+d.reason));
  }catch(e){toast('Report failed: '+e.message)}
  btn.disabled=false;
}

async function sendReminders(){
  const btn=document.getElementById('remind-btn');
  btn.disabled=true;btn.textContent='Sending…';
  try{
    const r=await fetch('/api/send-reminders?'+tok(),{method:'POST'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    toast(`Reminders for ${d.date}: ${d.sent} sent, ${d.failed} failed (${d.due} due)`);
    loadAppts();
  }catch(e){toast('Reminders failed: '+e.message)}
  btn.textContent='Send Reminders';btn.disabled=false;
}

// ---- Today tab ----
async function loadToday(){
  try{
    const [kpis,appts,callsData]=await Promise.all([
      getJSON('/api/kpis?'+tok()),
      getJSON('/api/appointments?'+tok()),
      getJSON('/api/calls?'+tok({limit:100,offset:0}))
    ]);
    const tr=kpis.daily_trend||[];
    const todayEntry=tr[tr.length-1]||{};
    const bookedToday=todayEntry.booked||0;
    const callsToday=kpis.calls_today||0;
    const convRate=callsToday?Math.round(bookedToday/callsToday*100):0;

    // Tomorrow in browser local time — close enough for same-city clinic staff
    const tmr=new Date();tmr.setDate(tmr.getDate()+1);
    const tmrStr=tmr.toLocaleDateString('en-CA'); // YYYY-MM-DD
    const tmrAppts=appts.filter(a=>a.start_local&&a.start_local.slice(0,10)===tmrStr);
    const tmrLabel=tmr.toLocaleDateString('en-US',{weekday:'long',month:'short',day:'numeric'});

    const flagged=(callsData.rows||[]).filter(r=>r.attention).slice(0,5);

    let h=`<div style="padding:1rem 1.5rem">
    <div class="kpi-grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin-bottom:1.5rem">
      <div class="kpi"><div class="kpi-val">${callsToday}</div><div class="kpi-lbl">Calls Today</div></div>
      <div class="kpi"><div class="kpi-val" style="color:#34c759">${bookedToday}</div><div class="kpi-lbl">Booked Today</div></div>
      <div class="kpi"><div class="kpi-val">${convRate}%</div><div class="kpi-lbl">Conversion</div></div>
      <div class="kpi"><div class="kpi-val" style="color:${flagged.length?'#ff9500':'#34c759'}">${flagged.length}</div><div class="kpi-lbl">Needs Attention</div></div>
    </div>
    <div class="kpi-lbl" style="margin-bottom:.6rem">Tomorrow — ${esc(tmrLabel)} &nbsp;·&nbsp; ${tmrAppts.length} appointment${tmrAppts.length!==1?'s':''}</div>`;

    if(tmrAppts.length){
      h+=`<table><thead><tr><th>Time</th><th>Patient</th><th>Service</th><th>Status</th><th>Reminder</th></tr></thead><tbody>`;
      for(const a of tmrAppts){
        const t=a.start_local?new Date(a.start_local).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'}):'—';
        const confirmed=a.status==='confirmed';
        h+=`<tr>
          <td style="white-space:nowrap;font-weight:500">${esc(t)}</td>
          <td>${esc(a.name)}</td><td>${esc(a.service||'—')}</td>
          <td><span class="badge b-${confirmed?'confirmed':'other'}">${esc(a.status||'—')}</span></td>
          <td>${a.reminder_sent?'<span style="color:#34c759;font-size:.8rem">✓ sent</span>':'<span class="no-sum">pending</span>'}</td>
        </tr>`;
      }
      h+=`</tbody></table>`;
    }else{
      h+=`<div style="padding:.75rem 0;color:#aeaeb2;font-size:.85rem">No appointments scheduled for tomorrow.</div>`;
    }

    if(flagged.length){
      h+=`<div class="kpi-lbl" style="margin:1.5rem 0 .6rem">Needs Attention</div>
      <table><thead><tr><th>Time</th><th>Phone</th><th>Outcome</th><th>Reason</th></tr></thead><tbody>`;
      for(const r of flagged){
        h+=`<tr>
          <td style="white-space:nowrap">${ts(r.ended_at)}</td>
          <td>${esc(r.phone)}</td><td>${badge(r.outcome)}</td>
          <td style="font-size:.75rem;color:#991b1b">${esc((r.attention_reasons||[]).join(' · '))}</td>
        </tr>`;
      }
      h+=`</tbody></table>`;
    }else if(callsToday>0){
      h+=`<div style="margin-top:1.25rem;padding:.75rem 1rem;background:#f0fdf4;border-radius:8px;font-size:.85rem;color:#065f46">All clear — no calls need attention today.</div>`;
    }
    h+=`</div>`;
    document.getElementById('today-content').innerHTML=h;
  }catch(e){
    document.getElementById('today-content').innerHTML='<div class="empty">'+esc(e.message)+'</div>';
  }
}

let _lastRefresh=0;
function loadAll(){
  loadKpis();loadCalls();loadAppts();loadIssues();loadWaitlist();
  if(document.getElementById('tab-today').classList.contains('active'))loadToday();
  _lastRefresh=Date.now();
}
loadAll();
setInterval(loadAll,60000);
setInterval(()=>{
  if(!_lastRefresh)return;
  const el=document.getElementById('refresh-ts');if(!el)return;
  const s=Math.round((Date.now()-_lastRefresh)/1000);
  el.textContent=s<5?'just updated':`updated ${s}s ago`;
},1000);
</script>
</body>
</html>"""
