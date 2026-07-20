"""
Dashboard authentication: JWT sessions + DB-persisted brute-force throttle.

JWT (HS256) stored in an HttpOnly cookie:
  sub   = str(user_id)
  role  = "admin" | "staff"
  jti   = random UUID — stored in revoked_tokens on logout/password-change
  iat   = issued-at
  exp   = issued-at + SESSION_HOURS

Throttle: LoginAttempt rows in DB (survives Render restarts; was in-memory).
Legacy:   shared DASHBOARD_TOKEN (?token= / x-dashboard-token header) kept
          for cron jobs / API scripts — rotate once real users exist.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import hmac
import logging
import os
import uuid

import jwt as _jwt

logger = logging.getLogger("clinic")

SESSION_COOKIE = "dash_session"
SESSION_HOURS  = 12
_PBKDF2_ITERS  = 200_000
_ALGORITHM     = "HS256"
_MAX_ATTEMPTS  = 8
_WINDOW        = dt.timedelta(seconds=900)   # 15 min


def _secret() -> str:
    s = os.getenv("DASHBOARD_SECRET") or os.getenv("PHONE_HASH_HMAC_KEY") or ""
    if not s:
        raise RuntimeError("Set DASHBOARD_SECRET (or PHONE_HASH_HMAC_KEY) for JWT signing")
    return s


# ---------- passwords ----------

def hash_password(password: str) -> str:
    import secrets
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                 _PBKDF2_ITERS).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                    _PBKDF2_ITERS).hex()
    return hmac.compare_digest(candidate, digest)


# ---------- JWT ----------

def make_token(user_id: int, role: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub":  str(user_id),
        "role": role,
        "jti":  str(uuid.uuid4()),
        "iat":  now,
        "exp":  now + dt.timedelta(hours=SESSION_HOURS),
    }
    return _jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def verify_token(token: str | None, session) -> dict | None:
    """Return {user_id, role, jti} for a valid non-revoked token, else None."""
    if not token:
        return None
    try:
        payload = _jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    except _jwt.PyJWTError:
        return None
    jti = payload.get("jti")
    if jti and _is_revoked(jti, session):
        return None
    return {
        "user_id": int(payload["sub"]),
        "role":    payload["role"],
        "jti":     jti,
    }


def revoke_token(jti: str | None, session) -> None:
    """Blacklist a jti so it can never be used again (logout / password change)."""
    if not jti:
        return
    from .models import RevokedToken
    # Prune expired entries (older than SESSION_HOURS + 1h buffer) lazily
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(hours=SESSION_HOURS + 1))
    session.query(RevokedToken).filter(RevokedToken.revoked_at < cutoff).delete()
    if not _is_revoked(jti, session):
        session.add(RevokedToken(jti=jti))


def _is_revoked(jti: str, session) -> bool:
    from .models import RevokedToken
    return session.get(RevokedToken, jti) is not None


# ---------- DB throttle ----------

def throttled(key: str, session) -> bool:
    from .models import LoginAttempt
    cutoff = dt.datetime.now(dt.timezone.utc) - _WINDOW
    count = (session.query(LoginAttempt)
             .filter(LoginAttempt.key == key,
                     LoginAttempt.failed_at >= cutoff)
             .count())
    return count >= _MAX_ATTEMPTS


def record_failure(key: str, session) -> None:
    from .models import LoginAttempt
    # Prune rows older than 2× window to keep the table small
    cutoff = dt.datetime.now(dt.timezone.utc) - _WINDOW * 2
    session.query(LoginAttempt).filter(LoginAttempt.failed_at < cutoff).delete()
    session.add(LoginAttempt(key=key))


def clear_failures(key: str, session) -> None:
    from .models import LoginAttempt
    session.query(LoginAttempt).filter(LoginAttempt.key == key).delete()


# ---------- user helpers ----------

def authenticate(session_db, username: str, password: str):
    """Return the DashboardUser on success, else None."""
    from .models import DashboardUser
    user = (session_db.query(DashboardUser)
            .filter_by(username=(username or "").strip().lower(), active=True)
            .one_or_none())
    if user is None:
        # Burn time so missing usernames aren't distinguishable from wrong passwords
        verify_password(password, "0" * 32 + "$" + "0" * 64)
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    return user


def create_user(session_db, *, username: str, password: str,
                role: str = "staff", clinic_id: int = 1):
    from .models import DashboardUser
    username = (username or "").strip().lower()
    if not username or len(password or "") < 8:
        raise ValueError("username required; password must be 8+ characters")
    if role not in ("admin", "staff"):
        raise ValueError("role must be admin or staff")
    if session_db.query(DashboardUser).filter_by(username=username).one_or_none():
        raise ValueError("username already exists")
    user = DashboardUser(username=username, password_hash=hash_password(password),
                         role=role, clinic_id=clinic_id)
    session_db.add(user)
    return user


def change_password(session_db, user_id: int,
                    current_password: str, new_password: str) -> None:
    """Logged-in user changes their own password. Raises ValueError on failure."""
    from .models import DashboardUser
    user = session_db.get(DashboardUser, user_id)
    if user is None:
        raise ValueError("user not found")
    if not verify_password(current_password, user.password_hash):
        raise ValueError("current password is incorrect")
    if len(new_password or "") < 8:
        raise ValueError("new password must be at least 8 characters")
    user.password_hash       = hash_password(new_password)
    user.password_changed_at = dt.datetime.now(dt.timezone.utc)


def reset_password(session_db, user_id: int, new_password: str) -> None:
    """Admin resets another user's password — no current password required."""
    from .models import DashboardUser
    user = session_db.get(DashboardUser, user_id)
    if user is None:
        raise ValueError("user not found")
    if len(new_password or "") < 8:
        raise ValueError("new password must be at least 8 characters")
    user.password_hash       = hash_password(new_password)
    user.password_changed_at = dt.datetime.now(dt.timezone.utc)


def bootstrap_admin(session_factory) -> None:
    """Create the first admin from env if the users table is empty."""
    from .models import DashboardUser
    username = os.getenv("DASHBOARD_ADMIN_USER")
    password = os.getenv("DASHBOARD_ADMIN_PASSWORD")
    if not (username and password):
        return
    with session_factory() as session:
        if session.query(DashboardUser).count() > 0:
            return
        try:
            create_user(session, username=username, password=password, role="admin")
            session.commit()
            logger.info("[AUTH] bootstrap admin '%s' created", username)
        except ValueError as e:
            logger.error("[AUTH] bootstrap admin failed: %s", e)
