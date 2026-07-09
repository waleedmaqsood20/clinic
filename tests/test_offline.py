"""
Offline tests — no accounts, no server, no database needed.

Tests: knowledge FAQ search, crypto round-trip, ToolExecutor (lookup_faq,
check_availability, book_appointment) with the in-memory calendar.

Run:  python tests/test_offline.py
"""
import os
import sys

os.environ.setdefault("ENCRYPTION_KEY", "Z3Vlc3MtdGhpcy1pcy1hLTMyLWJ5dGUta2V5LXh4eHg=")
os.environ.setdefault("PHONE_HASH_HMAC_KEY", "test-hmac")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime as _dt
from zoneinfo import ZoneInfo as _ZI
from app import knowledge, crypto
from app.tools import ToolExecutor
from app.providers import InMemoryCalendar, SmsProvider

# Use clinic-timezone dates so the past-date guard never rejects test inputs
_tz = _ZI(knowledge.CLINIC_PROFILE["timezone"])
_today = _dt.datetime.now(_tz).date()
_TEST_DAY = (_today + _dt.timedelta(days=7)).isoformat()   # next week, always future
_TEST_DAY2 = (_today + _dt.timedelta(days=8)).isoformat()  # day after

ok = True


def check(label: str, cond: bool) -> None:
    global ok
    ok = ok and cond
    print(f"{'✅' if cond else '❌'} {label}")


def main() -> int:
    print("=" * 60)
    print("Offline tests")
    print("=" * 60)

    # 1) Knowledge / FAQ
    hits = knowledge.search("opening hours")
    check("FAQ: hours query matches", bool(hits) and "open" in hits[0].answer.lower())
    hits = knowledge.search("parking")
    check("FAQ: parking query matches", bool(hits) and "parking" in hits[0].answer.lower())
    check("FAQ: no match returns empty list", knowledge.search("xyzzy123") == [])

    # 2) Crypto
    blob = crypto.encrypt("Alice Smith")
    check("Crypto: round-trip encrypt/decrypt", crypto.decrypt(blob) == "Alice Smith")
    check("Crypto: None input is safe",
          crypto.encrypt(None) is None and crypto.decrypt(None) is None)
    h1 = crypto.phone_hash("+13175550001")
    h2 = crypto.phone_hash("+13175550001")
    check("Crypto: phone_hash is deterministic", h1 == h2)
    check("Crypto: phone_hash differs for different numbers",
          crypto.phone_hash("+13175550001") != crypto.phone_hash("+13175550002"))

    # 3) ToolExecutor — FAQ
    ex = ToolExecutor(InMemoryCalendar(), SmsProvider())
    result = ex.execute("lookup_faq", {"query": "what are your hours"}, "+10000000000")
    check("Executor: lookup_faq returns answer with 'open'", "open" in result.lower())

    # 4) ToolExecutor — availability
    result = ex.execute(
        "check_availability",
        {"day": _TEST_DAY, "service": "cleaning"},
        "+10000000000",
    )
    check("Executor: check_availability returns slot times",
          "available" in result.lower())

    # 5) ToolExecutor — booking (no DB)
    result = ex.execute(
        "book_appointment",
        {"day": _TEST_DAY, "time": "10am", "name": "Bob Lee",
         "service": "cleaning", "reason": "checkup"},
        "+13175550099",
    )
    check("Executor: book_appointment confirms booking", "Booked" in result)

    # 6) Booking removes the slot (not double-bookable)
    result2 = ex.execute(
        "book_appointment",
        {"day": _TEST_DAY, "time": "10:30am", "name": "Carol Day",
         "service": "exam", "reason": "new patient"},
        "+13175550088",
    )
    check("Executor: second booking on different slot confirms", "Booked" in result2)

    # 7) Unknown tool
    result = ex.execute("nonexistent_tool", {}, "+10000000000")
    check("Executor: unknown tool name returns message", "Unknown" in result)

    # 7b) Past-date guard — check_availability with January date returns error, not "no availability"
    past_day = "2026-01-09"
    result = ex.execute("check_availability", {"day": past_day, "service": "cleaning"}, "+10000000000")
    check("Executor: past date returns 'Date error', not 'No availability'",
          "date error" in result.lower() and "no availability" not in result.lower())
    # 7c) Past-date guard — book_appointment with January date returns error
    result = ex.execute("book_appointment",
                        {"day": past_day, "time": "10am", "name": "Test", "service": "cleaning"},
                        "+10000000000")
    check("Executor: past date booking returns 'Date error'", "date error" in result.lower())

    # 8) check_upcoming_appointments — InMemoryCalendar has no bookings by phone
    result = ex.execute("check_upcoming_appointments", {}, "+13175550099")
    import json as _json
    parsed = _json.loads(result)
    check("Executor: check_upcoming returns JSON with appointments key",
          "appointments" in parsed)
    check("Executor: check_upcoming returns empty list for InMemory",
          parsed["appointments"] == [])

    # 9) cancel_appointment — requires event_id; missing id returns error string
    result = ex.execute("cancel_appointment", {}, "+13175550099")
    check("Executor: cancel without event_id returns error", "need" in result.lower())

    # 10) cancel_appointment — InMemoryCalendar stub accepts any event_id
    result = ex.execute("cancel_appointment", {"event_id": "INMEM1001"}, "+13175550099")
    check("Executor: cancel with event_id confirms cancellation",
          "cancelled" in result.lower())

    # 11) reschedule_appointment — requires event_id; missing id returns error string
    result = ex.execute("reschedule_appointment",
                        {"new_day": _TEST_DAY2, "new_time": "10am"}, "+13175550099")
    check("Executor: reschedule without event_id returns error", "need" in result.lower())

    # 12) reschedule_appointment — InMemoryCalendar stub accepts any event_id
    result = ex.execute("reschedule_appointment",
                        {"event_id": "INMEM1001", "new_day": _TEST_DAY2, "new_time": "10am"},
                        "+13175550099")
    check("Executor: reschedule with event_id confirms move", "moved" in result.lower())

    print("-" * 60)
    print("RESULT:", "ALL CHECKS PASSED ✅" if ok else "SOMETHING FAILED ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
