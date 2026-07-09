"""
In-memory cache for week availability, keyed by Retell call_id.

Populated by the call_started webhook before the LLM is invoked.
Consumed by _week_availability() in tools.py for a sub-100ms response.
Falls back to a live fetch if the cache misses (race on cold start).
"""
from __future__ import annotations
import threading
import time

_lock = threading.Lock()
_cache: dict[str, tuple[dict, float]] = {}
TTL = 600  # 10 minutes


def put(call_id: str, week_data: dict) -> None:
    _evict_expired_locked()
    with _lock:
        _cache[call_id] = (week_data, time.monotonic() + TTL)


def get(call_id: str | None) -> dict | None:
    if not call_id:
        return None
    with _lock:
        entry = _cache.get(call_id)
        if entry is None:
            return None
        data, expires = entry
        if time.monotonic() > expires:
            del _cache[call_id]
            return None
        return data


def _evict_expired_locked() -> None:
    now = time.monotonic()
    with _lock:
        expired = [k for k, (_, exp) in _cache.items() if now > exp]
        for k in expired:
            del _cache[k]
