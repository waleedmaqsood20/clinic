"""
Dashboard authentication: per-user logins with signed session cookies.

- Passwords: PBKDF2-HMAC-SHA256, 200k iterations, per-user salt (stdlib only).
- Sessions: HMAC-signed token in an HttpOnly cookie, 12h expiry, no server
  state (survives Render restarts).
- Legacy: the shared DASHBOARD_TOKEN still authenticates (header or ?token=)
  so existing bookmarks and cron jobs keep working. Token access has admin
  rights — rotate it once real users are set up.
- Bootstrap: at startup, if no users exist and DASHBOARD_ADMIN_USER +
  DASHBOARD_ADMIN_PASSWORD are set, the first admin is created.
"""
from __future__ import annotations
import base64
import datetime as dt
import hashlib
import hmac
import logging
import os
import secrets
import time

logger = logging.getLogger("clinic")

SESSION_COOKIE = "dash_session"
SESSION_HOURS = 12
_PBKDF2_ITERS = 200_000

# naive in-memory login throttle: {key: (fail_count, first_fail_ts)}
_attempts: dict[str, tuple[int, float]] = {}
_MAX_ATTEMPTS = 8
_WINDOW_S = 900


def _secret() -> bytes:
    s = os.getenv("DASHBOARD_SECRET") or os.getenv("PHONE_HASH_HMAC_KEY") or ""
    if not s:
        raise RuntimeError("Set DASHBOARD_SECRET (or PHONE_HASH_HMAC_KEY) for sessions")
    return s.encode()


# ---------- passwords ----------

def hash_password(password: str) -> str:
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


# ---------- sessions ----------

def make_session(user_id: int, role: str) -> str:
    expires = int(time.time()) + SESSION_HOURS * 3600
    nonce = secrets.token_hex(8)
    payload = f"{user_id}.{role}.{expires}.{nonce}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def read_session(token: str | None) -> dict | None:
    """Return {'user_id', 'role'} for a valid unexpired session, else None."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        parts = raw.split(".")
        if len(parts) != 5:
            return None
        user_id, role, expires, nonce, sig = parts
        payload = f"{user_id}.{role}.{expires}.{nonce}"
        expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(expires) < time.time():
            return None
        return {"user_id": int(user_id), "role": role}
    except Exception:
        return None


# ---------- login throttle ----------

def throttled(key: str) -> bool:
    count, first = _attempts.get(key, (0, 0.0))
    if time.time() - first > _WINDOW_S:
        return False
    return count >= _MAX_ATTEMPTS


def record_failure(key: str) -> None:
    count, first = _attempts.get(key, (0, 0.0))
    if time.time() - first > _WINDOW_S:
        _attempts[key] = (1, time.time())
    else:
        _attempts[key] = (count + 1, first)


def clear_failures(key: str) -> None:
    _attempts.pop(key, None)


# ---------- user helpers ----------

def authenticate(session_db, username: str, password: str):
    """Return the DashboardUser on success, else None."""
    from .models import DashboardUser
    user = (session_db.query(DashboardUser)
            .filter_by(username=(username or "").strip().lower(), active=True)
            .one_or_none())
    if user is None:
        # burn comparable time so missing users aren't distinguishable
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
