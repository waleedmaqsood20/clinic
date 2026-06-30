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

    def cancel(self, caller_phone: str) -> str:
        """Cancel next upcoming appointment. Returns appointment id, 'no_contact', or 'no_appointment'."""
        return "no_appointment"

    def reschedule(self, caller_phone: str, new_slot: Slot) -> str:
        """Move next upcoming appointment to new_slot. Returns appointment id or error sentinel."""
        return "no_appointment"


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

    def _get_upcoming_appointment(self, contact_id: str) -> dict | None:
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
            return None
        if resp.status_code != 200:
            raise RuntimeError(
                f"GHL calendar events lookup failed {resp.status_code}: {resp.text[:300]}"
            )
        body = resp.json() or {}
        events = body.get("events") or []
        logger.info("GHL calendar events: total=%d, searching for contactId=%s", len(events), contact_id)
        cancelled_statuses = {"cancelled", "completed", "noshow", "invalid"}
        for e in events:
            if e.get("contactId") == contact_id:
                status = e.get("appointmentStatus", "")
                if status not in cancelled_statuses:
                    logger.info("GHL found appointment id=%s status=%s startTime=%s",
                                e.get("id"), status, e.get("startTime"))
                    return e
                logger.info("GHL appointment id=%s skipped (status=%s)", e.get("id"), status)
        logger.info("GHL no upcoming appointment found for contact %s among %d events",
                    contact_id, len(events))
        return None

    def cancel(self, caller_phone: str) -> str | dict:
        logger.info("GHL cancel: phone=%s", caller_phone)
        contact_id = self._find_contact_by_phone(caller_phone)
        if not contact_id:
            logger.info("GHL cancel: no contact found → no_contact")
            return "no_contact"
        appt = self._get_upcoming_appointment(contact_id)
        if not appt:
            logger.info("GHL cancel: no upcoming appt for contact %s → no_appointment", contact_id)
            return "no_appointment"
        appt_id = appt.get("id", "")
        resp = self.client.put(
            f"{self.BASE}/calendars/events/appointments/{appt_id}",
            headers=self._headers(),
            json={"appointmentStatus": "cancelled"},
        )
        logger.info("GHL cancel PUT %s → %s: %s", appt_id, resp.status_code, resp.text[:200])
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"GHL cancel failed {resp.status_code}: {resp.text}")
        return appt  # full dict so caller gets title + startTime in response

    def reschedule(self, caller_phone: str, new_slot: Slot) -> str:
        logger.info("GHL reschedule: phone=%s", caller_phone)
        contact_id = self._find_contact_by_phone(caller_phone)
        if not contact_id:
            logger.info("GHL reschedule: no contact found → no_contact")
            return "no_contact"
        appt = self._get_upcoming_appointment(contact_id)
        if not appt:
            logger.info("GHL reschedule: no upcoming appt for contact %s → no_appointment", contact_id)
            return "no_appointment"
        appt_id = appt.get("id", "")
        contact_id = appt.get("contactId") or appt.get("contact_id") or ""
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
            "contactId": contact_id,
            "startTime": start_dt.isoformat(),
            "endTime": end_dt.isoformat(),
            "appointmentStatus": "confirmed",
        }
        resp = self.client.put(
            f"{self.BASE}/calendars/events/appointments/{appt_id}",
            headers=self._headers(), json=body,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"GHL reschedule failed {resp.status_code}: {resp.text}")
        return appt_id

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
