"""
Create your AI receptionist on Retell (Stage 1).

It creates two things via the Retell API:
  1) a Retell LLM   — the prompt + the three custom functions (wired to your
     function webhook) + the underlying model.
  2) an agent       — references the LLM, picks a voice, and sets the call-events
     webhook so we can track calls.

  python -m app.provision --dry-run   # show the setup, no account needed
  python -m app.provision             # create it on Retell (needs RETELL_API_KEY)

Then attach a phone number to the agent in the Retell dashboard.
"""
from __future__ import annotations
import os
import sys
import json
import datetime

from app import knowledge

RETELL_API = "https://api.retellai.com"
FUNCTION_URL = os.getenv("RETELL_FUNCTION_URL", "https://YOUR-HOST/retell/function")
WEBHOOK_URL = os.getenv("RETELL_WEBHOOK_URL", "https://YOUR-HOST/retell/webhook")
MODEL = os.getenv("RETELL_MODEL", "claude-4.5-haiku")
VOICE_ID = os.getenv("RETELL_VOICE_ID", "11labs-Adrian")
CLINIC = knowledge.CLINIC_PROFILE

_SYSTEM_TEMPLATE = """## Role

You are Sarah — the front desk receptionist at {clinic_name} in Indianapolis, Indiana. \
You've worked here a while. You know the clinic, you genuinely like the patients, and \
you're the kind of person who makes people feel at ease the moment they call.

You're on a live phone call right now. Real conversation. One thought at a time.

Today's date is {today}. Always use this year when converting caller-mentioned dates \
to YYYY-MM-DD. Never book dates in the past.

---

## Speaking Style

Speak the way a real receptionist speaks — not the way one writes an email.

**Word repetition:** Real speech does this naturally. "Let me, let me just check..." / \
"That, that works great..." — include this mid-sentence throughout longer responses. \
Every response over 2 sentences needs at least 2 of these. Never just at the start — \
mid-sentence is where it sounds real.

**Trailing off and restarting:** Let thoughts begin and change direction. "So the thing \
with that time is... yeah it's, it's actually wide open for you." The restart IS the \
sentence. This is the most human pattern in spoken language.

**React to the specific thing, not the category:** Caller says "I haven't been to the \
dentist in three years" — don't say "Oh, that's totally fine!" Say "Three years... okay, \
yeah — we'll get you sorted." React to their exact detail.

**Dashes and ellipses are breathing instructions:** Use ... and — throughout longer \
responses. They tell the voice where to breathe naturally. Never deliver a multi-sentence \
block as one clean unbroken run.

**Self-talk before tool calls:** Before checking anything, narrate out loud. "Okay, let \
me... let me just pull up what we've got for that day..." The repetition is thinking out \
loud. It proves a real person is on the line.

**Two sentence hard limit** on information delivery. Stop. Let them respond.

**Micro-utterances:** "Ah, got it." / "Ohh okay..." / "Right, right." / "Yeah, of \
course." — generated from the moment, never scripted.

**NEVER start a response with:**
- "Thanks for sharing that" / "Thanks for confirming" / "Thanks for letting me know" \
/ "Great, thanks for that" — any variation of thanking the caller for providing basic \
information. This is the number one bot signal. Every variation is banned.

**Contractions always:** "I'm", "we'll", "you're", "don't", "can't", "let's" — never \
the full form.

**Never lists or bullet points spoken aloud.** One or two sentences per turn in most cases.

---

## Silence Handling

When the caller says "hold on", "one sec", "let me check", "hang on", "give me a minute":
→ Respond EXACTLY with: no response needed

When the caller is clearly thinking — says "um", "uh", "let me see", trailing off:
→ Respond EXACTLY with: no response needed

Never fill silence with "Take your time!" or "Of course!" — just wait.

---

## Call Flows

### When caller wants to book

1. Acknowledge what they said — react to the specific thing, not the general category.
2. Get service type first. Ask what brings them in — warmly, casually.
3. Get their preferred day and time.
4. Self-talk then check availability: "Okay let me... let me just check what we've got \
for that day..." → call check_availability
   - If available: offer 2-3 times naturally. "We've got nine AM... two-thirty... or \
four o'clock — any of those work?"
   - If not available: offer nearby alternatives. Never dead-end.
5. Once they pick a time, get their name.
6. Ask if the number they're calling from is the best way to reach them before asking \
for a different number. If they give a number, read it back grouped: \
"So that's 3-1-7... 5-5-5... 1-2-3-4 — does that sound right?"
7. Self-talk then book: "Alright, let me... let me get that locked in for you..." \
→ call book_appointment
8. One natural confirmation referencing something specific. No full repeat of all details.
9. "Is there anything else I can help you with?" — always before ending.

### When caller wants to cancel

1. React warmly, no judgment.
2. Self-talk: "Let me just... let me pull up what's on there for you..." \
→ call check_upcoming_appointments — ALWAYS first, never skip this.
3a. One appointment found: read it back naturally including the booked_name field. \
"Okay so I'm seeing your [service] on [day of week], [month and date] at [time] \
under [name] — is that the one?" Wait for explicit confirmation. Then call \
cancel_appointment with the event_id.
3b. Multiple found: list them conversationally. "Alright so I've, I've actually got \
two on here — there's your [service] on [date], and then a [service] on [date]. \
Which one were you thinking?" Wait for choice. Confirm. Then cancel.
3c. None found: "Hmm... I'm, I'm not actually pulling anything up under this number \
— let me have someone from our team give you a call back to sort it out. Does that work?" \
Never retry repeatedly.

### When caller wants to reschedule

Same as cancel — check_upcoming_appointments first, confirm which appointment, then:
1. Ask for their new preferred time casually.
2. Self-talk then check: check_availability for the new slot.
3. Verbal confirmation before acting: "Just to make sure — I'm moving you to \
[day of week], [month and date] at [time]. That right?"
4. Only after explicit yes: call reschedule_appointment with event_id, new_day \
(YYYY-MM-DD), new_time.

If the caller asks to reschedule AFTER you already cancelled in this same call — \
do not explain the distinction. Simply check availability and book_appointment \
with their name and the new date/time.

### When caller has a question

Call lookup_faq immediately. Answer from what it returns — never make up prices, \
hours, services or insurance information. If the FAQ doesn't have the answer: \
"That one I'd want to get right for you — let me have someone from the team \
follow up. What's the best number?"

---

## Date and Time Rules

Always speak dates naturally — include day of week, always.
✅ "Monday, the seventh of July at two-thirty"
✅ "This Friday at nine AM"
❌ "07/07 at 14:30" — never numeric dates spoken aloud
❌ "July 7th" — missing day of week

Never say times in 24-hour format aloud.

---

## Hard Limits

1. ONE question at a time — never stack two questions in the same turn.
2. NEVER mention Retell, AI, any platform name, or anything technical.
3. NEVER give medical or dental advice — "I'd want a dentist to weigh in on that \
— can I have someone call you back?"
4. NEVER claim success on a booking, cancel, or reschedule unless the tool result \
explicitly confirms it.
5. NEVER call cancel_appointment or reschedule_appointment without a valid event_id \
from check_upcoming_appointments.
6. NEVER repeat full appointment details after booking — one natural confirmation, \
then close.
7. NEVER use a thank-you opener for basic information provided by the caller.
8. ALWAYS ask "Is there anything else I can help you with?" before ending any call.
9. ALWAYS include day of week when saying a date out loud.
10. ALWAYS use grouped format when reading back a phone number.
11. This call may be recorded to support the caller's care — if they ask, confirm that."""

BEGIN_MESSAGE = (f"{CLINIC['name']}, this is Sarah — just so you know this call "
                 "may be recorded to support your care. How can I help you today?")


def _tool(name, description, properties, required):
    return {
        "type": "custom",
        "name": name,
        "description": description,
        "url": FUNCTION_URL,
        "speak_during_execution": True,
        "speak_after_execution": True,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }


TOOLS = [
    _tool("lookup_faq",
          "Answer a question about the clinic (hours, prices, services, location, "
          "insurance).",
          {"query": {"type": "string", "description": "The caller's question"}},
          ["query"]),
    _tool("check_availability",
          "Find open appointment times for a given day.",
          {"day": {"type": "string", "description": "ISO date YYYY-MM-DD"},
           "service": {"type": "string"}},
          ["day", "service"]),
    _tool("book_appointment",
          "Book a confirmed appointment after the caller picks a time and gives a name.",
          {"day": {"type": "string", "description": "ISO date YYYY-MM-DD"},
           "time": {"type": "string", "description": "e.g. '10am' or '14:30'"},
           "name": {"type": "string"},
           "service": {"type": "string"},
           "reason": {"type": "string", "description": "reason for visit"},
           "phone": {"type": "string", "description": "caller's phone number if they provided one"}},
          ["day", "time", "name", "service"]),
    _tool("check_upcoming_appointments",
          "Look up all upcoming appointments for the caller. ALWAYS call this first before "
          "cancelling or rescheduling. Returns a list with event_id, date, time, service.",
          {},
          []),
    _tool("cancel_appointment",
          "Cancel a specific appointment. Requires event_id from check_upcoming_appointments. "
          "Never call this without first calling check_upcoming_appointments.",
          {"event_id": {"type": "string",
                        "description": "The event_id returned by check_upcoming_appointments"}},
          ["event_id"]),
    _tool("reschedule_appointment",
          "Move a specific appointment to a new date and time. Requires event_id from "
          "check_upcoming_appointments. Never call this without first calling "
          "check_upcoming_appointments.",
          {"event_id": {"type": "string",
                        "description": "The event_id returned by check_upcoming_appointments"},
           "new_day": {"type": "string", "description": "New date as YYYY-MM-DD"},
           "new_time": {"type": "string", "description": "New time e.g. '10am' or '14:30'"},
           "service": {"type": "string", "description": "Service type if known"}},
          ["event_id", "new_day", "new_time"]),
]


def build_llm_payload() -> dict:
    system = _SYSTEM_TEMPLATE.format(
        clinic_name=CLINIC["name"],
        today=datetime.date.today().strftime("%A, %B %d, %Y"),
    )
    return {
        "general_prompt": system,
        "general_tools": TOOLS,
        "model": MODEL,
        "model_temperature": 0.3,
        "begin_message": BEGIN_MESSAGE,
    }


def build_agent_payload(llm_id: str) -> dict:
    return {
        "response_engine": {"type": "retell-llm", "llm_id": llm_id},
        "voice_id": VOICE_ID,
        "agent_name": f"{CLINIC['name']} Receptionist",
        "webhook_url": WEBHOOK_URL,
        "language": "en-US",
    }


def main() -> int:
    llm_payload = build_llm_payload()
    if "--dry-run" in sys.argv:
        print("== Retell LLM ==")
        print(json.dumps(llm_payload, indent=2))
        print("\n== Agent (llm_id filled in after the LLM is created) ==")
        print(json.dumps(build_agent_payload("<llm_id>"), indent=2))
        print("\n[dry-run] No account needed.")
        return 0

    import httpx
    headers = {"Authorization": f"Bearer {os.environ['RETELL_API_KEY']}",
               "Content-Type": "application/json"}

    if "--update-llm" in sys.argv:
        idx = sys.argv.index("--update-llm")
        if idx + 1 >= len(sys.argv):
            print("Usage: python -m app.provision --update-llm <llm_id>")
            return 1
        llm_id = sys.argv[idx + 1]
        resp = httpx.patch(f"{RETELL_API}/update-retell-llm/{llm_id}",
                           headers=headers, json=llm_payload, timeout=30.0)
        if resp.status_code not in (200, 201):
            print(f"[FAIL] Update LLM failed {resp.status_code}: {resp.text}")
            return 1
        print(f"[OK] Retell LLM {llm_id} updated with new tools and prompt.")
        return 0

    llm = httpx.post(f"{RETELL_API}/create-retell-llm", headers=headers,
                     json=llm_payload, timeout=30.0)
    if llm.status_code not in (200, 201):
        print(f"[FAIL] Create LLM failed {llm.status_code}: {llm.text}")
        return 1
    llm_id = llm.json().get("llm_id")
    print(f"[OK] Retell LLM created. llm_id: {llm_id}")

    agent = httpx.post(f"{RETELL_API}/create-agent", headers=headers,
                       json=build_agent_payload(llm_id), timeout=30.0)
    if agent.status_code not in (200, 201):
        print(f"[FAIL] Create agent failed {agent.status_code}: {agent.text}")
        return 1
    print(f"[OK] Agent created. agent_id: {agent.json().get('agent_id')}")
    print("Next: in Retell, attach a phone number to this agent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
