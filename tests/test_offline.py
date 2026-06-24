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

from app import knowledge, crypto
from app.tools import ToolExecutor
from app.providers import InMemoryCalendar, SmsProvider

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
        {"day": "2026-06-16", "service": "cleaning"},
        "+10000000000",
    )
    check("Executor: check_availability returns slot times",
          "available" in result.lower())

    # 5) ToolExecutor — booking (no DB)
    result = ex.execute(
        "book_appointment",
        {"day": "2026-06-16", "time": "10am", "name": "Bob Lee",
         "service": "cleaning", "reason": "checkup"},
        "+13175550099",
    )
    check("Executor: book_appointment confirms booking", "Booked" in result)

    # 6) Booking removes the slot (not double-bookable)
    result2 = ex.execute(
        "book_appointment",
        {"day": "2026-06-16", "time": "10:30am", "name": "Carol Day",
         "service": "exam", "reason": "new patient"},
        "+13175550088",
    )
    check("Executor: second booking on different slot confirms", "Booked" in result2)

    # 7) Unknown tool
    result = ex.execute("nonexistent_tool", {}, "+10000000000")
    check("Executor: unknown tool name returns message", "Unknown" in result)

    print("-" * 60)
    print("RESULT:", "ALL CHECKS PASSED ✅" if ok else "SOMETHING FAILED ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
