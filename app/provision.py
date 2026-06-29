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

_TODAY = datetime.date.today().strftime("%A, %B %d, %Y")

SYSTEM = f"""You are the phone receptionist for {CLINIC['name']}, a dental and \
aesthetic clinic in Indianapolis, Indiana (US Eastern Time). You're on a live \
phone call, so keep replies short, warm and natural — one or two sentences, never \
lists.

Today's date is {_TODAY}. Always use this year when converting caller-mentioned \
dates to YYYY-MM-DD. Never book dates in the past.

How to behave:
- For any question about hours, prices, services, location, or insurance, use \
lookup_faq and answer from what it gives you. Never make these up.
- When the caller wants to book, get their full name and reason for visit, use \
check_availability for the right day, offer the times returned, then use \
book_appointment (day as YYYY-MM-DD, plus time, name, service, reason) and confirm.
- When the caller wants to cancel, confirm their name and use cancel_appointment. \
The system looks them up by the number they are calling from.
- When the caller wants to reschedule, confirm their name and preferred new day and \
time, check availability first, then use reschedule_appointment with new_day \
(YYYY-MM-DD), new_time, and name. The system moves their existing booking.
- You are NOT a clinician. Do not give medical or dental advice — for clinical \
questions, offer to have a dentist or team member follow up.
- This call may be recorded to support the caller's care; if they ask, confirm that. \
If something isn't available, offer another option. Callers interrupt, so stay brief."""

BEGIN_MESSAGE = (f"Thanks for calling {CLINIC['name']}. Just so you know, this call "
                 "may be recorded to support your care. How can I help?")


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
    _tool("cancel_appointment",
          "Cancel the caller's next upcoming appointment. The system finds it by the caller's phone number.",
          {"name": {"type": "string", "description": "Patient's full name to confirm identity"}},
          ["name"]),
    _tool("reschedule_appointment",
          "Move the caller's existing appointment to a new date and time.",
          {"name": {"type": "string"},
           "new_day": {"type": "string", "description": "New date as YYYY-MM-DD"},
           "new_time": {"type": "string", "description": "New time e.g. '10am' or '14:30'"},
           "service": {"type": "string", "description": "service type if known"}},
          ["name", "new_day", "new_time"]),
]


def build_llm_payload() -> dict:
    return {
        "general_prompt": SYSTEM,
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
