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
import logging
import re
import datetime as dt
from zoneinfo import ZoneInfo

from . import knowledge
from .providers import CalendarProvider, SmsProvider, Slot

logger = logging.getLogger("clinic")


# ---------- small date/time helpers ----------
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _fmt_day(d: dt.date) -> str:
    """'Tuesday Jun 16' — no leading zero, cross-platform (avoids %-d on Windows)."""
    return f"{d.strftime('%A %b')} {d.day}"


def _fmt_time(t: dt.datetime) -> str:
    """'10am' or '2:30pm' — no leading zero, cross-platform (avoids %-I on Windows)."""
    hour = t.hour % 12 or 12
    ap = "am" if t.hour < 12 else "pm"
    return f"{hour}:{t.minute:02d}{ap}" if t.minute else f"{hour}{ap}"


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
            phone = args.get("phone") or caller_phone
            return self._book(args.get("day", ""), args.get("time", ""),
                              args.get("name", "the caller"),
                              args.get("service", "exam"),
                              args.get("reason", ""), phone, call_id)
        if name == "cancel_appointment":
            return self._cancel(args.get("name", "the caller"), caller_phone)
        if name == "reschedule_appointment":
            return self._reschedule(args.get("name", "the caller"),
                                    args.get("new_day", ""), args.get("new_time", ""),
                                    args.get("service", ""), caller_phone)
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
            return f"No availability on {_fmt_day(day)}. Offer another day."
        times = ", ".join(_fmt_time(s.start) for s in slots[:6])
        return f"Available on {_fmt_day(day)}: {times}."

    def _book(self, day_str: str, time_str: str, name: str, service: str,
              reason: str, caller_phone: str, call_id) -> str:
        day = _parse_day(day_str)
        slots = self.calendar.availability(day, service)
        slot = _match_time(slots, time_str)
        if not slot:
            return "That time isn't available — offer the caller another slot."
        conf = self.calendar.book(slot, name, caller_phone, service)
        when = f"{_fmt_day(slot.start)} at {_fmt_time(slot.start)}"

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

    def _cancel(self, name: str, caller_phone: str) -> str:
        result = self.calendar.cancel(caller_phone)
        if result == "no_contact":
            return ("I couldn't find a record for this number. "
                    "Could you confirm the phone number you booked with?")
        if result == "no_appointment":
            return (f"I don't see any upcoming appointments for {name}. "
                    "Is it possible it was booked under a different number?")
        title = (result.get("title") or "appointment").split(" - ")[0].strip()
        raw = result.get("startTime") or result.get("start_time") or ""
        try:
            start_dt = dt.datetime.fromisoformat(raw)
            when = f"{_fmt_day(start_dt.date())} at {_fmt_time(start_dt)}"
        except (ValueError, TypeError):
            when = "the scheduled time"
        return (f"Done, {name} — your {title} on {when} has been cancelled. "
                "We hope to see you again soon.")

    def _reschedule(self, name: str, day_str: str, time_str: str,
                    service: str, caller_phone: str) -> str:
        day = _parse_day(day_str)
        slots = self.calendar.availability(day, service or "exam")
        slot = _match_time(slots, time_str)
        if not slot:
            return "That time isn't available — offer the caller another slot."
        result = self.calendar.reschedule(caller_phone, slot)
        if result == "no_contact":
            return "I couldn't find a patient record for this number."
        if result == "no_appointment":
            return (f"I don't see any upcoming appointments for {name}. "
                    "Is it possible it was booked under a different number?")
        when = f"{_fmt_day(slot.start)} at {_fmt_time(slot.start)}"
        return f"Done, {name} — your appointment has been moved to {when}."


# ---------- the function Retell talks to ----------
def _infer_function(body: dict) -> tuple[str, dict]:
    """
    Retell sends {name: "fn_name", args: {...}, call: {...}}.
    Use name/args directly when present; fall back to flat-arg inference.
    """
    fn_name = body.get("name", "")
    nested_args = body.get("args")
    if fn_name and isinstance(nested_args, dict):
        return fn_name, nested_args

    # Flat format fallback: infer function from which keys are present
    meta = {"tool_call_id", "execution_message", "call", "name", "args"}
    flat = {k: v for k, v in body.items() if k not in meta}
    if "query" in flat:
        return "lookup_faq", flat
    if "new_day" in flat or "new_time" in flat:
        return "reschedule_appointment", flat
    if "time" in flat or ("day" in flat and "name" in flat):
        return "book_appointment", flat
    if "day" in flat:
        return "check_availability", flat
    if "name" in flat:
        return "cancel_appointment", flat
    return fn_name or "unknown", flat


def handle_function_call(body: dict, executor: ToolExecutor) -> str:
    """
    Retell sends ONE custom-function call. Newer Retell LLM sends args flat
    (no name/args/call envelope). We infer the function from which args exist.
    """
    call = body.get("call") or {}
    caller = call.get("from_number") or "+10000000000"
    call_id = call.get("call_id")
    fn_name, args = _infer_function(body)
    try:
        out = executor.execute(fn_name, args, caller, call_id)
    except Exception:
        logger.exception("[RETELL] tool %s failed", fn_name)
        out = ("I'm having a little trouble with that right now. "
               "I'll make sure someone from our team follows up with you shortly.")
    return str(out).replace("\n", " ").strip()
