"""
Outside-world providers: GoHighLevel (GHL) calendar + CRM, and SMS.

Each has an interface and a fake in-memory version for offline tests. The real
GHL integration is hidden behind the same simple shape so the rest of the code
doesn't care which calendar is used. The brain only ever talks to the interface.
"""
from __future__ import annotations
import datetime as dt
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

logger = logging.getLogger("clinic")


# ---------- Calendar ----------

@dataclass
class Slot:
    start: dt.datetime           # local (clinic timezone), for matching + speaking
    duration_min: int = 30
    iso_utc: str | None = None   # original ISO string from the provider, used to book


class CalendarProvider:
    def availability(self, day: dt.date, service: str) -> list[Slot]:
        raise NotImplementedError

    def book(self, slot: Slot, name: str, phone: str, service: str) -> str:
        """Return a confirmation id."""
        raise NotImplementedError

    def get_upcoming_appointments(self, caller_phone: str) -> list[dict]:
        """Return list of upcoming appointment dicts (id, startTime, title, appointmentStatus)."""
        return []

    def get_week_availability(self, service: str = "") -> dict:
        """Return {YYYY-MM-DD: [Slot, ...]} for the next 7 days."""
        return {}

    def cancel(self, event_id: str) -> str:
        """Cancel appointment by event_id. Returns 'cancelled' or raises RuntimeError."""
        return "no_appointment"

    def reschedule(self, event_id: str, new_slot: Slot) -> str:
        """Move appointment to new_slot by event_id. Returns event_id or raises RuntimeError."""
        return "no_appointment"

    def get_appointment(self, event_id: str) -> dict | None:
        """Fetch one appointment by id. Returns the event dict, or None if not found."""
        return None

    def add_contact_note(self, phone: str, note: str) -> bool:
        """Attach a note to the CRM contact for this phone. Returns True on success."""
        return False


class InMemoryCalendar(CalendarProvider):
    """Fake calendar (open 9-17, 30-min slots). Used when no GHL keys are set."""

    def __init__(self) -> None:
        self.bookings: list[dict] = []
        self._taken: set[dt.datetime] = set()

    def availability(self, day: dt.date, service: str) -> list[Slot]:
        if day.weekday() == 6:  # Sunday closed
            return []
        slots = []
        for hour in range(9, 17):
            for minute in (0, 30):
                start = dt.datetime.combine(day, dt.time(hour, minute))
                if start not in self._taken:
                    slots.append(Slot(start))
        return slots

    def book(self, slot: Slot, name: str, phone: str, service: str) -> str:
        self._taken.add(slot.start)
        conf = f"INMEM{1000 + len(self.bookings) + 1}"
        self.bookings.append({"id": conf, "start": slot.start, "name": name,
                              "phone": phone, "service": service})
        return conf

    def get_upcoming_appointments(self, caller_phone: str) -> list[dict]:
        return []

    def get_week_availability(self, service: str = "") -> dict:
        result = {}
        today = dt.date.today()
        for i in range(7):
            day = today + dt.timedelta(days=i)
            slots = self.availability(day, service)
            if slots:
                result[day.isoformat()] = slots
        return result

    def cancel(self, event_id: str) -> str:
        return "cancelled"

    def reschedule(self, event_id: str, new_slot: Slot) -> str:
        return event_id

    def get_appointment(self, event_id: str) -> dict | None:
        for b in self.bookings:
            if b["id"] == event_id:
                return {"id": event_id, "appointmentStatus": "confirmed"}
        return None

    def add_contact_note(self, phone: str, note: str) -> bool:
        self.bookings.append({"note_for": phone, "note": note})
        return True


class GHLCalendar(CalendarProvider):
    """
    Real GoHighLevel (LeadConnector) v2 integration.

      availability -> GET  /calendars/{calendarId}/free-slots
      book         -> POST /contacts/upsert  (find/create the patient in the CRM)
                      then POST /calendars/events/appointments

    Auth: a Private Integration token (Bearer) + the 'Version: 2021-07-28' header.
    Free slots come back as a map keyed by date (YYYY-MM-DD), each with a 'slots'
    list of ISO datetimes in the requested timezone.

    `client` is any object exposing .get(url, headers, params) and
    .post(url, headers, json) returning a response with .status_code, .json()
    and .text — i.e. an httpx.Client, or a fake for tests.
    """

    BASE = "https://services.leadconnectorhq.com"
    VERSION = "2021-07-28"

    def __init__(self, token: str, location_id: str, calendar_id: str,
                 timezone: str = "America/Indiana/Indianapolis",
                 slot_minutes: int = 30, client=None) -> None:
        self.token = token
        self.location_id = location_id
        self.calendar_id = calendar_id
        self.timezone = timezone
        self.slot_minutes = slot_minutes
        if client is None:
            import httpx
            client = httpx.Client(timeout=12.0)
        self.client = client

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Version": self.VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json"}

    def availability(self, day: dt.date, service: str) -> list[Slot]:
        tz = ZoneInfo(self.timezone)
        start_ms = int(dt.datetime.combine(day, dt.time(0, 0), tz).timestamp() * 1000)
        end_ms = int(dt.datetime.combine(day, dt.time(23, 59), tz).timestamp() * 1000)
        resp = self.client.get(
            f"{self.BASE}/calendars/{self.calendar_id}/free-slots",
            headers=self._headers(),
            params={"startDate": start_ms, "endDate": end_ms, "timezone": self.timezone},
        )
        if resp.status_code != 200:
            logger.error("GHL free-slots failed %s: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json() or {}
        out: list[Slot] = []
        for key, value in data.items():
            if not (len(key) == 10 and key[4] == "-"):   # only YYYY-MM-DD keys
                continue
            for iso in (value.get("slots", []) if isinstance(value, dict) else []):
                start = dt.datetime.fromisoformat(iso)
                out.append(Slot(start=start, duration_min=self.slot_minutes, iso_utc=iso))
        out.sort(key=lambda s: s.start)
        return out

    @staticmethod
    def _to_e164(phone: str) -> str | None:
        """Normalise to E.164 (+1XXXXXXXXXX for US). Returns None if not parseable."""
        import re
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) == 10:
            return f"+1{digits}"          # bare 10-digit US number
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"           # 1XXXXXXXXXX already has country code
        if phone.startswith("+") and len(digits) >= 8:
            return f"+{digits}"           # already has + prefix
        return None

    def _upsert_contact(self, name: str, phone: str) -> str | None:
        first, _, last = (name or "").partition(" ")
        body: dict = {"locationId": self.location_id,
                      "firstName": first or name,
                      "lastName": last or first}
        e164 = self._to_e164(phone or "")
        if e164 and not e164.startswith("+10000000000"):
            body["phone"] = e164
        resp = self.client.post(f"{self.BASE}/contacts/upsert",
                                headers=self._headers(), json=body)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"GHL contact upsert failed {resp.status_code}: {resp.text}")
        d = resp.json() or {}
        contact_id = (d.get("contact") or {}).get("id") or d.get("id")
        if not contact_id:
            raise RuntimeError(f"GHL contact upsert returned no id. Response: {d}")
        return contact_id

    def _find_contact_by_phone(self, phone: str) -> str | None:
        e164 = self._to_e164(phone or "")
        if not e164:
            logger.warning("GHL contact search: could not normalize phone %r", phone)
            return None
        # POST /contacts/search is the current non-deprecated endpoint
        r = self.client.post(
            f"{self.BASE}/contacts/search",
            headers=self._headers(),
            json={"locationId": self.location_id, "query": e164, "pageLimit": 5},
        )
        logger.info("GHL contact search %s → %s: %s", e164, r.status_code, r.text[:300])
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RuntimeError(
                f"GHL contacts/search failed {r.status_code}: {r.text[:300]}"
            )
        d = r.json() or {}
        contacts = d.get("contacts") or []
        contact_id = contacts[0].get("id") if contacts else None
        logger.info("GHL contact search result: found=%s id=%s", bool(contact_id), contact_id)
        return contact_id

    def _fetch_calendar_events(self) -> list[dict]:
        """Fetch all active calendar events for the next 90 days."""
        now = dt.datetime.now(dt.timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        end_ms = int((now + dt.timedelta(days=90)).timestamp() * 1000)
        resp = self.client.get(
            f"{self.BASE}/calendars/events",
            headers=self._headers(),
            params={
                "locationId": self.location_id,
                "calendarId": self.calendar_id,
                "startTime": now_ms,
                "endTime": end_ms,
            },
        )
        logger.info("GHL calendar events → %s: %s", resp.status_code, resp.text[:400])
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise RuntimeError(
                f"GHL calendar events lookup failed {resp.status_code}: {resp.text[:300]}"
            )
        body = resp.json() or {}
        events = body.get("events") or []
        logger.info("GHL calendar events total=%d", len(events))
        return events

    def get_week_availability(self, service: str = "") -> dict:
        """Fetch open slots for the next 7 days in a single GHL free-slots call."""
        tz = ZoneInfo(self.timezone)
        today = dt.date.today()
        start_ms = int(dt.datetime.combine(today, dt.time(0, 0), tz).timestamp() * 1000)
        end_ms = int(dt.datetime.combine(
            today + dt.timedelta(days=7), dt.time(23, 59), tz).timestamp() * 1000)
        resp = self.client.get(
            f"{self.BASE}/calendars/{self.calendar_id}/free-slots",
            headers=self._headers(),
            params={"startDate": start_ms, "endDate": end_ms, "timezone": self.timezone},
        )
        if resp.status_code != 200:
            logger.error("GHL week slots failed %s: %s", resp.status_code, resp.text[:200])
            return {}
        data = resp.json() or {}
        result = {}
        for key, value in data.items():
            if not (len(key) == 10 and key[4] == "-"):
                continue
            slots = []
            for iso in (value.get("slots", []) if isinstance(value, dict) else []):
                start_dt = dt.datetime.fromisoformat(iso)
                slots.append(Slot(start=start_dt, duration_min=self.slot_minutes, iso_utc=iso))
            if slots:
                result[key] = sorted(slots, key=lambda s: s.start)
        return result

    def get_upcoming_appointments(self, caller_phone: str) -> list[dict]:
        """Return all upcoming appointments for this phone number as raw GHL event dicts."""
        logger.info("GHL get_upcoming_appointments: phone=%s", caller_phone)
        contact_id = self._find_contact_by_phone(caller_phone)
        if not contact_id:
            logger.info("GHL get_upcoming_appointments: no contact found")
            return []
        events = self._fetch_calendar_events()
        cancelled_statuses = {"cancelled", "completed", "noshow", "invalid"}
        results = []
        for e in events:
            if e.get("contactId") == contact_id:
                status = e.get("appointmentStatus", "")
                if status not in cancelled_statuses:
                    logger.info("GHL upcoming: id=%s status=%s startTime=%s",
                                e.get("id"), status, e.get("startTime"))
                    results.append(e)
        logger.info("GHL get_upcoming_appointments: found %d for contact %s", len(results), contact_id)
        return results

    def cancel(self, event_id: str) -> str:
        """Cancel appointment by event_id. Returns 'cancelled' or raises RuntimeError."""
        logger.info("GHL cancel: event_id=%s", event_id)
        resp = self.client.put(
            f"{self.BASE}/calendars/events/appointments/{event_id}",
            headers=self._headers(),
            json={"appointmentStatus": "cancelled"},
        )
        logger.info("GHL cancel PUT %s → %s: %s", event_id, resp.status_code, resp.text[:200])
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"GHL cancel failed {resp.status_code}: {resp.text}")
        return "cancelled"

    def reschedule(self, event_id: str, new_slot: Slot) -> str:
        """Move appointment to new_slot by event_id. Returns event_id or raises RuntimeError."""
        logger.info("GHL reschedule: event_id=%s", event_id)
        tz = ZoneInfo(self.timezone)
        if new_slot.iso_utc:
            start_dt = dt.datetime.fromisoformat(new_slot.iso_utc)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=tz)
        else:
            start_dt = (new_slot.start.replace(tzinfo=tz)
                        if new_slot.start.tzinfo is None else new_slot.start)
        end_dt = start_dt + dt.timedelta(minutes=self.slot_minutes)
        body: dict = {
            "calendarId": self.calendar_id,
            "locationId": self.location_id,
            "startTime": start_dt.isoformat(),
            "endTime": end_dt.isoformat(),
            "appointmentStatus": "confirmed",
        }
        resp = self.client.put(
            f"{self.BASE}/calendars/events/appointments/{event_id}",
            headers=self._headers(), json=body,
        )
        logger.info("GHL reschedule PUT %s → %s: %s", event_id, resp.status_code, resp.text[:200])
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"GHL reschedule failed {resp.status_code}: {resp.text}")
        return event_id

    def get_appointment(self, event_id: str) -> dict | None:
        """GET one appointment by id — used for post-call booking verification."""
        resp = self.client.get(
            f"{self.BASE}/calendars/events/appointments/{event_id}",
            headers=self._headers())
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(
                f"GHL get appointment failed {resp.status_code}: {resp.text[:200]}")
        d = resp.json() or {}
        return d.get("appointment") or d.get("event") or d

    def _get_contact_info(self, contact_id: str) -> tuple[str | None, str | None]:
        """Fetch a contact by ID and return (phone, full_name)."""
        if not contact_id:
            return None, None
        r = self.client.get(f"{self.BASE}/contacts/{contact_id}",
                            headers=self._headers())
        if r.status_code != 200:
            logger.warning("GHL get contact %s → %s", contact_id, r.status_code)
            return None, None
        d = (r.json() or {}).get("contact") or r.json() or {}
        phone = d.get("phone") or None
        name = (d.get("name")
                or " ".join(filter(None, [d.get("firstName"), d.get("lastName")]))
                or None)
        return phone, name or None

    def _get_contact_phone(self, contact_id: str) -> str | None:
        phone, _ = self._get_contact_info(contact_id)
        return phone

    def fetch_calendar_events_range(self, days_back: int = 120,
                                    days_ahead: int = 90) -> list[dict]:
        """Fetch GHL calendar events spanning past + future (for sync)."""
        now = dt.datetime.now(dt.timezone.utc)
        start_ms = int((now - dt.timedelta(days=days_back)).timestamp() * 1000)
        end_ms = int((now + dt.timedelta(days=days_ahead)).timestamp() * 1000)
        resp = self.client.get(
            f"{self.BASE}/calendars/events",
            headers=self._headers(),
            params={"locationId": self.location_id,
                    "calendarId": self.calendar_id,
                    "startTime": start_ms, "endTime": end_ms},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"GHL calendar events failed {resp.status_code}: {resp.text[:300]}")
        return (resp.json() or {}).get("events") or []

    def add_contact_note(self, phone: str, note: str) -> bool:
        """Attach an intake note (reason/insurance) to the CRM contact.

        Uses POST /contacts/{id}/notes — works on any GHL account without
        pre-configured custom fields. Failure never breaks a booking.
        """
        try:
            contact_id = self._find_contact_by_phone(phone)
            if not contact_id:
                logger.warning("GHL note: no contact found for %s", phone)
                return False
            resp = self.client.post(
                f"{self.BASE}/contacts/{contact_id}/notes",
                headers=self._headers(), json={"body": note})
            if resp.status_code not in (200, 201):
                logger.error("GHL note failed %s: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception:
            logger.exception("GHL add_contact_note failed")
            return False

    def book(self, slot: Slot, name: str, phone: str, service: str) -> str:
        contact_id = self._upsert_contact(name, phone)
        tz = ZoneInfo(self.timezone)
        if slot.iso_utc:
            start_dt = dt.datetime.fromisoformat(slot.iso_utc)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=tz)
        else:
            start_dt = slot.start.replace(tzinfo=tz) if slot.start.tzinfo is None else slot.start
        end_dt = start_dt + dt.timedelta(minutes=self.slot_minutes)
        # GHL requires numeric timezone offset (e.g. -04:00), not Z
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()
        body: dict = {
            "calendarId": self.calendar_id,
            "locationId": self.location_id,   # required per GHL official schema
            "contactId": contact_id,
            "startTime": start_iso,
            "endTime": end_iso,
            "appointmentStatus": "confirmed",
            "title": f"{service} - {name}",
        }
        resp = self.client.post(f"{self.BASE}/calendars/events/appointments",
                                headers=self._headers(), json=body)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"GHL booking failed: {resp.status_code} {resp.text} | payload: {body}")
        d = resp.json() or {}
        return str(d.get("id") or (d.get("appointment") or {}).get("id") or "BOOKED")


# ---------- SMS (optional; GHL can also send its own confirmations) ----------

@dataclass
class SmsProvider:
    sent: list[dict] = field(default_factory=list)

    def send(self, to: str, body: str) -> None:
        self.sent.append({"to": to, "body": body})


class TwilioSms(SmsProvider):
    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        super().__init__()
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number

    def send(self, to: str, body: str) -> None:
        from twilio.rest import Client
        Client(self.account_sid, self.auth_token).messages.create(
            to=to, from_=self.from_number, body=body)
        self.sent.append({"to": to, "body": body})
