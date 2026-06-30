"""
Stage 1 test — exercises the REAL FastAPI app end to end, offline (Retell + GHL).

Covers: the Retell function endpoint, booking persistence (encrypted), call
tracking from Retell's call_analyzed event, the staff dashboard, AND the GHL
calendar provider request-building (with a fake HTTP client).

Run:  python tests/test_stage1.py     (needs: pip install -r requirements.txt)
"""
import os, sys, tempfile, datetime as dt

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmpdb}",
    "ENCRYPTION_KEY": "Z3Vlc3MtdGhpcy1pcy1hLTMyLWJ5dGUta2V5LXh4eHg=",
    "PHONE_HASH_HMAC_KEY": "test-hmac",
    "DASHBOARD_TOKEN": "dash-secret",
    "CLINIC_TZ": "America/Indiana/Indianapolis",
    # NOTE: no RETELL_API_KEY -> signature check skipped; no GHL_* -> in-memory calendar
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.server import app, SessionLocal
from app import repository, crypto
from app.providers import GHLCalendar

client = TestClient(app)
CALL_ID = "retell-call-abc"
PHONE = "+13175551234"

ok = True
def check(label, cond):
    global ok; ok = ok and cond
    print(f"{'✅' if cond else '❌'} {label}")


def fn(name, args):
    return {"name": name, "args": args,
            "call": {"from_number": PHONE, "call_id": CALL_ID}}


# ---- a fake GHL HTTP client to verify request building ----
class FakeResp:
    def __init__(self, status, payload, text=""):
        self.status_code = status; self._p = payload; self.text = text
    def json(self): return self._p

class FakeGHL:
    def __init__(self): self.calls = []
    def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url, headers, params))
        if "calendars/events" in url and "free-slots" not in url:
            return FakeResp(200, {"events": [
                {"id": "evt_789", "contactId": "contact_123",
                 "appointmentStatus": "confirmed",
                 "startTime": "2026-07-06T09:00:00-04:00",
                 "title": "cleaning - Test User"}
            ]})
        return FakeResp(200, {"2026-06-16": {"slots":
            ["2026-06-16T10:00:00-04:00", "2026-06-16T10:30:00-04:00"]}, "traceId": "x"})
    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers, json))
        if url.endswith("/contacts/upsert"):
            return FakeResp(200, {"contact": {"id": "contact_123"}})
        if url.endswith("/contacts/search"):
            return FakeResp(200, {"contacts": [{"id": "contact_123"}]})
        if url.endswith("/calendars/events/appointments"):
            return FakeResp(201, {"id": "appt_456"})
        return FakeResp(404, {}, "nope")
    def put(self, url, headers=None, json=None):
        self.calls.append(("PUT", url, headers, json))
        return FakeResp(200, {"id": url.rstrip("/").split("/")[-1]})


def main():
    print("=" * 60)

    # 1) Crypto
    blob = crypto.encrypt("Sarah Jones")
    check("Encryption round-trips", crypto.decrypt(blob) == "Sarah Jones")
    check("Phone hash deterministic", crypto.phone_hash(PHONE) == crypto.phone_hash(PHONE))

    # 2) FAQ via the Retell function endpoint
    r = client.post("/retell/function", json=fn("lookup_faq", {"query": "opening hours"}))
    check("Function endpoint returns 200", r.status_code == 200)
    result_text = r.json().get("result", "") if isinstance(r.json(), dict) else r.json()
    check("FAQ result is a string with hours", "open" in result_text.lower())

    # 3) Booking persists (encrypted) + audit
    r = client.post("/retell/function", json=fn("book_appointment",
        {"day": "2026-06-16", "time": "10am", "name": "Sarah Jones",
         "service": "cleaning", "reason": "tooth pain"}))
    result_text = r.json().get("result", "") if isinstance(r.json(), dict) else r.json()
    check("Booking result confirms", "Booked" in result_text)
    with SessionLocal() as s:
        from app.models import Appointment, AuditLog
        appt = s.query(Appointment).filter_by(call_id=CALL_ID).one_or_none()
        check("Appointment row persisted", appt is not None)
        if appt:
            check("Name stored encrypted + decryptable",
                  crypto.decrypt(appt.caller_name_enc) == "Sarah Jones")
            check("Reason captured", crypto.decrypt(appt.reason_enc) == "tooth pain")
            check("Phone stored as keyed hash",
                  appt.phone_hash == crypto.phone_hash(PHONE))
        check("Audit row for booking",
              s.query(AuditLog).filter_by(action="appointment.created").count() == 1)

    # 4) Call tracking from Retell call_analyzed (background task runs in TestClient)
    now_ms = int(dt.datetime.now().timestamp() * 1000)
    evt = {"event": "call_analyzed", "call": {
        "call_id": CALL_ID, "from_number": PHONE,
        "transcript": "Agent: hello...\nUser: book a cleaning",
        "disconnection_reason": "user_hangup",
        "start_timestamp": now_ms, "end_timestamp": now_ms + 47000,
        "recording_url": "https://recordings.example/abc.wav",
        "call_cost": {"combined_cost": 0.12},
        "call_analysis": {"call_summary": "Caller booked a cleaning for a sore tooth.",
                          "call_successful": True}}}
    r = client.post("/retell/webhook", json=evt)
    check("Webhook accepts call_analyzed (200)", r.status_code == 200)
    with SessionLocal() as s:
        from app.models import Call
        c = s.query(Call).filter_by(call_id=CALL_ID).one_or_none()
        check("Call row persisted", c is not None)
        if c:
            check("Outcome is 'booked'", c.outcome == "booked")
            check("Duration computed (~47s)", c.duration_seconds == 47)
            check("Summary stored encrypted + decryptable",
                  crypto.decrypt(c.summary_enc) == "Caller booked a cleaning for a sore tooth.")
            check("Recording reference stored", (c.recording_ref or "").endswith("abc.wav"))

    # 5) Dashboard
    r = client.get("/api/calls")
    check("Dashboard rejects missing token (401)", r.status_code == 401)
    r = client.get("/api/calls", headers={"x-dashboard-token": "dash-secret"})
    rows = r.json()
    check("Dashboard lists the call", r.status_code == 200 and len(rows) == 1)
    if rows:
        check("Phone masked to last 4",
              rows[0]["phone"].endswith("1234") and "317555" not in rows[0]["phone"])

    # 6) GHL provider builds the right requests
    ghl = GHLCalendar(token="tok", location_id="loc", calendar_id="cal",
                      timezone="America/Indiana/Indianapolis", client=FakeGHL())
    slots = ghl.availability(dt.date(2026, 6, 16), "cleaning")
    check("GHL availability parses 2 slots", len(slots) == 2 and slots[0].start.hour == 10)
    get_call = ghl.client.calls[0]
    check("GHL free-slots URL + timezone param",
          "free-slots" in get_call[1] and get_call[3]["timezone"].startswith("America/"))
    check("GHL auth + Version header",
          get_call[2]["Authorization"].startswith("Bearer ") and
          get_call[2]["Version"] == "2021-07-28")
    conf = ghl.book(slots[0], "Sarah Jones", PHONE, "cleaning")
    check("GHL booking returns appointment id", conf == "appt_456")
    posts = [c for c in ghl.client.calls if c[0] == "POST"]
    check("GHL upserts the contact first",
          posts[0][1].endswith("/contacts/upsert") and posts[0][3]["phone"] == PHONE
          and posts[0][3]["firstName"] == "Sarah")
    check("GHL creates appointment with contactId + startTime",
          posts[1][1].endswith("/calendars/events/appointments") and
          posts[1][3]["contactId"] == "contact_123" and
          posts[1][3]["calendarId"] == "cal" and
          posts[1][3]["startTime"] == "2026-06-16T10:00:00-04:00")

    # 7) GHL check_upcoming_appointments — phone lookup then calendar events
    ghl2 = GHLCalendar(token="tok", location_id="loc", calendar_id="cal",
                       timezone="America/Indiana/Indianapolis", client=FakeGHL())
    upcoming = ghl2.get_upcoming_appointments(PHONE)
    check("GHL get_upcoming_appointments returns list", isinstance(upcoming, list))
    check("GHL get_upcoming_appointments uses POST /contacts/search",
          any(c[0] == "POST" and "contacts/search" in c[1] for c in ghl2.client.calls))
    check("GHL get_upcoming_appointments uses GET /calendars/events",
          any(c[0] == "GET" and "calendars/events" in c[1] for c in ghl2.client.calls))

    # 8) GHL cancel by event_id
    ghl3 = GHLCalendar(token="tok", location_id="loc", calendar_id="cal",
                       timezone="America/Indiana/Indianapolis", client=FakeGHL())
    result = ghl3.cancel("evt_789")
    check("GHL cancel returns 'cancelled'", result == "cancelled")
    check("GHL cancel uses PUT with appointmentStatus",
          any(c[0] == "PUT" and c[3].get("appointmentStatus") == "cancelled"
              for c in ghl3.client.calls))

    # 9) GHL reschedule by event_id
    ghl4 = GHLCalendar(token="tok", location_id="loc", calendar_id="cal",
                       timezone="America/Indiana/Indianapolis", client=FakeGHL())
    new_slot = slots[1]  # 10:30am slot from earlier
    result = ghl4.reschedule("evt_789", new_slot)
    check("GHL reschedule returns event_id", result == "evt_789")
    check("GHL reschedule uses PUT with confirmed status",
          any(c[0] == "PUT" and c[3].get("appointmentStatus") == "confirmed"
              for c in ghl4.client.calls))

    print("-" * 60)
    print("RESULT:", "ALL CHECKS PASSED ✅" if ok else "SOMETHING FAILED ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
