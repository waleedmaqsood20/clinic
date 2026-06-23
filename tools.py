"""
The clinic's actions, and the function that answers Retell.

Retell runs the phone call and the brain. When the agent needs to DO something
(answer a question, check the diary, book), Retell calls our function webhook
with ONE function at a time ({name, args, call}). This code runs it and returns
a single short result string, which the agent then speaks.

Stage 1: every caller is treated as a new patient. We capture the caller's name,
phone (from caller ID), and reason for visit, and persist bookings + an audit
record. There is no patient lookup yet (that's Stage 2).
"""
from __future__ import annotations
import re
import datetime as dt
from zoneinfo import ZoneInfo

from . import knowledge
from .providers import CalendarProvider, SmsProvider, Slot


# ---------- small date/time helpers ----------
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_day(day_str: str) -> dt.date:
    s = (day_str or "").strip().lower()
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        pass
    today = dt.date.today()
    if "today" in s:
        return today
    if "tomorrow" in s:
        return today + dt.timedelta(days=1)
    for i, name in enumerate(_WEEKDAYS):
        if name in s:
            ahead = (i - today.weekday()) % 7
            return today + dt.timedelta(days=ahead or 7)
    return today + dt.timedelta(days=1)


def _match_time(slots: list[Slot], time_str: str) -> Slot | None:
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", (time_str or "").lower())
    if not m:
        return slots[0] if slots else None
    hour = int(m.group(1)); minute = int(m.group(2) or 0); ap = m.group(3)
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    for s in slots:
        if s.start.hour == hour and s.start.minute == minute:
            return s
    for s in slots:
        if s.start.hour == hour:
            return s
    return slots[0] if slots else None


def _slot_start_utc(slot: Slot) -> dt.datetime:
    if slot.iso_utc:
        try:
            return dt.datetime.fromisoformat(slot.iso_utc).astimezone(dt.timezone.utc)
        except ValueError:
            pass
    if slot.start.tzinfo:
        return slot.start.astimezone(dt.timezone.utc)
    tz = ZoneInfo(knowledge.CLINIC_PROFILE["timezone"])
    return slot.start.replace(tzinfo=tz).astimezone(dt.timezone.utc)


# ---------- the actions ----------
class ToolExecutor:
    def __init__(self, calendar: CalendarProvider, sms: SmsProvider,
                 session_factory=None) -> None:
        self.calendar = calendar
        self.sms = sms
        self.session_factory = session_factory

    def execute(self, name: str, args: dict, caller_phone: str, call_id=None) -> str:
        if name == "lookup_faq":
            return self._faq(args.get("query", ""))
        if name == "check_availability":
            return self._availability(args.get("day", ""), args.get("service", "exam"))
        if name == "book_appointment":
            return self._book(args.get("day", ""), args.get("time", ""),
                              args.get("name", "the caller"),
                              args.get("service", "exam"),
                              args.get("reason", ""), caller_phone, call_id)
        return f"Unknown tool {name}."

    def _faq(self, query: str) -> str:
        hits = knowledge.search(query)
        if hits:
            return hits[0].answer
        return ("I don't have that to hand — offer to take a message or arrange a "
                "callback.")

    def _availability(self, day_str: str, service: str) -> str:
        day = _parse_day(day_str)
        slots = self.calendar.availability(day, service)
        if not slots:
            return f"No availability on {day.strftime('%A %b %-d')}. Offer another day."
        times = ", ".join(s.start.strftime("%-I:%M%p").lower() for s in slots[:6])
        return f"Available on {day.strftime('%A %b %-d')}: {times}."

    def _book(self, day_str: str, time_str: str, name: str, service: str,
              reason: str, caller_phone: str, call_id) -> str:
        day = _parse_day(day_str)
        slots = self.calendar.availability(day, service)
        slot = _match_time(slots, time_str)
        if not slot:
            return "That time isn't available — offer the caller another slot."
        conf = self.calendar.book(slot, name, caller_phone, service)
        when = (slot.start.strftime("%A %b %-d at %-I:%M%p")
                .replace("AM", "am").replace("PM", "pm"))

        if self.session_factory:
            try:
                from . import repository
                with self.session_factory() as session:
                    repository.record_booking(
                        session, call_id=call_id, caller_phone=caller_phone,
                        name=name, service=service, reason=reason,
                        start_utc=_slot_start_utc(slot), confirmation=conf)
                    repository.write_audit(
                        session, actor="voice_ai", action="appointment.created",
                        call_id=call_id, phi=True,
                        detail={"service": service, "when": when})
                    session.commit()
            except Exception:
                pass

        try:
            self.sms.send(caller_phone,
                          f"{knowledge.CLINIC_PROFILE['name']}: {service} booked "
                          f"{when}. Ref {conf}.")
            note = "Confirmation sent by text."
        except Exception:
            note = ""
        return f"Booked {service} for {name} on {when}. Confirmation {conf}. {note}".strip()


# ---------- the function Retell talks to ----------
def handle_function_call(body: dict, executor: ToolExecutor) -> str:
    """
    Retell sends ONE custom-function call: {name, args, call}. We run it and
    return a single result string, which Retell hands back to the agent.
    """
    name = body.get("name")
    args = body.get("args") or {}
    call = body.get("call") or {}
    caller = call.get("from_number") or "+10000000000"
    call_id = call.get("call_id")
    try:
        out = executor.execute(name, args, caller, call_id)
    except Exception as e:
        out = f"Sorry, something went wrong: {e}"
    return str(out).replace("\n", " ").strip()
