# CLAUDE.md — Operational Reference for Bright Smile Dental Voice AI

Auto-loaded by Claude Code every session. Keep it current. Architecture details are in `CODE_GUIDE.md`. This file captures **live state, fixed bugs, API quirks, and gotchas** so we never re-discover the same issue twice.

---

## Project identity

| Item | Value |
|---|---|
| Clinic | Bright Smile Dental & Aesthetics, Indianapolis IN |
| Platform | Retell AI (voice/brain) + GoHighLevel GHL (calendar/CRM) |
| Deployed URL | `https://clinic-xprt.onrender.com` |
| GitHub | `https://github.com/waleedmaqsood20/clinic.git` |
| Retell LLM ID | `llm_9ea568d5a33e1c830c557936ad68` |
| Retell Agent ID | `agent_be261fdb7fa638f4d5fec96a5d` |
| Hosting | Render (free tier → **see Render gotchas below**) |
| Python env | `e:\clinicproject\clinic-1\venv\Scripts\python.exe` |

---

## Current working state (update this block after each session)

Last updated: 2026-07-09

| Feature | Status | Notes |
|---|---|---|
| FAQ lookup | ✅ Working | |
| Check availability | ✅ Working | |
| Book appointment | ✅ Working | GHL upsert + appointment create |
| Check upcoming appointments | ✅ Working | Returns event_id, date, time, service, booked_name |
| Cancel appointment | ✅ Live tested | Confirmed working with real caller (Alex, Jul 2026) |
| Reschedule appointment | ⚠️ Not tested | Code in place, never tested end-to-end |
| get_week_availability | ⚠️ Agent v5 live, not yet confirmed by real call | Prefetch cache wired; `call_started` webhook expected |
| SMS confirmation | ⏸ Paused | Client setting up Retell text integration |
| Staff dashboard | ✅ Live | `/dashboard?token=<DASHBOARD_TOKEN>` |
| Call tracking | ✅ Working | call_ended + call_analyzed + recording playback |
| Availability prefetch | ✅ Wired | call_started → cache → sub-100ms tool response |

---

## Critical: how Retell sends function calls

**Retell sends:** `{ "name": "fn_name", "args": { ...actual args... }, "call": { "from_number": "...", "call_id": "..." } }`

**Never infer the function name from args keys.** Use `body["name"]` and `body["args"]` directly.

The caller's phone number is **always** `body["call"]["from_number"]` — it is NOT in args.

### What broke (and the fix, commit f3166e0)

The old `_infer_function()` stripped `{tool_call_id, execution_message, call}` from the body but left `"name"` in the remaining dict. Since every Retell payload has a `"name"` key (the function name string like `"check_availability"`), the inference check `if "name" in args` always hit the `cancel_appointment` branch. Every single tool call — FAQ, availability, booking — returned "I couldn't find a record for this number."

**Fixed code in `app/tools.py:_infer_function()`:**
```python
fn_name = body.get("name", "")
nested_args = body.get("args")
if fn_name and isinstance(nested_args, dict):
    return fn_name, nested_args   # use Retell's own name/args — no inference needed
```
The flat-format fallback (below this) also strips `"name"` and `"args"` from the meta set.

---

## GHL API quirks

### Contact lookup: use `POST /contacts/search`

`/contacts/search/duplicate` is deprecated — unreliable for existing contact lookup.
`GET /contacts/` (list endpoint) is also deprecated.

Current `_find_contact_by_phone()` uses:
```
POST /contacts/search   body: {"locationId": ..., "query": <e164>, "pageLimit": 5}
```
Returns `{"contacts": [...]}`. Take `contacts[0]["id"]`.

### Appointment lookup: use `GET /calendars/events`, not `/contacts/{id}/appointments`

`GET /contacts/{contactId}/appointments` was returning empty even for known appointments — do not use.

Current `_fetch_calendar_events()` uses:
```
GET /calendars/events?locationId=...&calendarId=...&startTime=<now_ms>&endTime=<90d_ms>
```
Returns `{"events": [...]}`. Filter by `e["contactId"] == contact_id` and exclude statuses in `{cancelled, completed, noshow, invalid}`.

### Cancel: use `PUT`, not `DELETE`

`DELETE /calendars/events/appointments/{id}` does not work reliably. Use:
```
PUT /calendars/events/appointments/{id}   body: {"appointmentStatus": "cancelled"}
```

### Reschedule: `PUT` with new times + confirmed status
```
PUT /calendars/events/appointments/{id}
body: {"calendarId": ..., "locationId": ..., "startTime": ..., "endTime": ..., "appointmentStatus": "confirmed"}
```

### E.164 normalization

`_to_e164()` handles:
- 10-digit US → `+1XXXXXXXXXX`
- 11-digit starting with 1 → `+1XXXXXXXXXX`
- Already has `+` prefix (e.g., Australian `+61...`) → `+{digits}`

If the number can't be normalized, returns `None` and contact search is skipped.

### Auth headers (required on every GHL request)
```python
{"Authorization": f"Bearer {token}", "Version": "2021-07-28",
 "Content-Type": "application/json", "Accept": "application/json"}
```

---

## Render deployment gotchas

### Free tier wakes with old build

When Render's free tier service sleeps and wakes up, it restarts the **last successful build** — not the latest commit. A `git push` does trigger a rebuild, but if you pushed and the service just woke up before the rebuild finished, it runs old code.

**How to force a fresh deploy:** Push any change to GitHub (or use Render dashboard → Manual Deploy).

**How to tell which build is running:** Look for log lines that no longer exist in current code. If you see log messages that were removed in a commit, the old build is live.

### Free tier cold-start latency
First request after sleep takes ~10–15 seconds. Retell may timeout on the first function call after a cold start. Upgrade to a paid Render plan to eliminate sleep.

---

## ENCRYPTION_KEY format

`ENCRYPTION_KEY` in the environment is a **64-character hex string** (= 32 bytes when decoded with `bytes.fromhex()`), which is what AES-256-GCM requires.

`app/crypto.py:_key()` detects this automatically. Do NOT base64-encode a 64-char hex key — it will decode to the wrong length and fail with `AESGCM key must be 128, 192, or 256 bits`.

---

## Bugs fixed (chronological)

| Commit | Bug | Fix |
|---|---|---|
| `4e38ca6` | `ValueError: AESGCM key must be 128, 192, or 256 bits` | Detect 64-char hex key in `crypto.py:_key()` and use `bytes.fromhex()` |
| `9c9e840` | `Unknown tool cancel_appointment.` | Added cancel + reschedule handlers to `ToolExecutor.execute()` |
| `d6c6f44` | GHL errors silently returned `None` (hid real API failures) | Raise `RuntimeError` on unexpected status codes from GHL |
| `445080c` | `contacts/search/duplicate` didn't find existing contacts | Added fallback to `contacts/search` with phone as query |
| `f3166e0` | **ALL tools routed to cancel** (most critical bug) | Fixed `_infer_function()` to use `body["name"]`/`body["args"]` directly instead of inferring from remaining keys |
| `8910d3f` | Cancel returned "no appointment" despite appointment existing | Three stacked bugs: deprecated `/contacts/` → `POST /contacts/search`; deprecated `/contacts/{id}/appointments` → `GET /calendars/events`; `DELETE` → `PUT appointmentStatus=cancelled`. Also split cancel/reschedule into find-then-act (check_upcoming_appointments first) |
| `66232b3` | Agent didn't know name appointment was booked under; lectured caller about cancel vs reschedule | Added `booked_name` to check_upcoming JSON; system prompt now reads name back and treats reschedule-after-cancel as new booking |
| `d5cb696` | `get_week_availability` never fired — LLM invoked only after caller spoke, too late to silently prefetch | Removed `begin_message`; LLM now invoked at call-connect with empty conversation, calls tool first then generates greeting |
| `047715e` | Tool fetch caused dead air on Render cold start | `call_started` webhook triggers background GHL prefetch; `get_week_availability` returns from cache in <100ms |
| `33c00c1` | All changes since FIX 1 were never live — every `--update-llm` created a draft agent version that real calls never used | Published agent v5 via SDK; added `--deploy` command that patches LLM + publishes in one step; `--update-llm` now warns loudly |

---

## Pending work

- [x] **Live cancel test** — confirmed working with real caller (Jul 2026); GHL marks appointment as cancelled
- [x] **Deploy automation** — `--deploy` command added; `--update-llm` warns loudly if used alone
- [ ] **Agent v5 live-call test** — v5 never took a real call; confirm get_week_availability fires, no dead air, correct date, no "no response needed"
- [ ] **Cold-start test** — let Render sleep 15+ min, call, measure dead air before greeting (known risk of begin_message removal)
- [ ] **Slot-taken test** — book a slot manually, call and request it, confirm Sarah recovers with alternatives
- [ ] **Reschedule test** — book future appt, call to reschedule, verify GHL updates it (never tested end-to-end)
- [ ] **Render upgrade** — move to paid tier to eliminate cold-start sleep (real fix for cold-start dead air)
- [ ] **Rotate API keys** — rotate GHL token + Retell key (security hygiene)
- [ ] **SMS** — client setting up Retell text; wire in when ready
- [ ] **Key rotation** — `ENCRYPTION_KEY`, `PHONE_HASH_HMAC_KEY` to proper secrets manager

---

## How to test without making a live call

```powershell
# From project root
venv\Scripts\python.exe -m pytest tests\ -v
```

`tests/test_offline.py` — no accounts needed, exercises the full tool routing with a fake calendar.
`tests/test_stage1.py` — spins up real FastAPI app, uses fake HTTP client to stub GHL.

---

## How to deploy prompt/tool changes (update LLM + publish agent)

```powershell
$env:RETELL_API_KEY="your_key"
$env:RETELL_FUNCTION_URL="https://clinic-xprt.onrender.com/retell/function"
venv\Scripts\python.exe -m app.provision --deploy
```

`--deploy` does three things in one command:
1. PATCHes the LLM (new LLM version created)
2. Finds the auto-created draft agent version Retell generates after the PATCH
3. Publishes that draft — making it live for real calls immediately

**Do not use `--update-llm` alone.** It patches the LLM but leaves a draft agent version that real calls never touch. The flag now prints a loud warning if you run it by mistake.

### Why `--update-llm` alone is dangerous (the Jul 2026 incident)

Every `PATCH /update-retell-llm/{id}` creates a new LLM version *and* a new draft agent version. The phone number uses the **published** agent version, which is pinned to whatever LLM version it was published with. Running `--update-llm` without publishing means:
- All prompt changes, tool wiring changes, and `begin_message` changes are invisible to real calls
- The fallback still works (no crash), so you'd never notice from the caller experience
- Every "confirmed fix" you thought was live was actually sitting in a draft

This is what happened: agent v4 (LLM v4) was live for all real calls from the beginning of the sprint through Jul 9, 2026 — including the test calls that showed `get_week_availability` not firing and "no response needed" being spoken. None of the fixes from FIX 1–6 were deployed. Agent v5 (LLM v5) was published manually on Jul 9 via `retell_sdk.agent.publish(agent_id, version=5)`.

---

## Adding a new tool (checklist)

1. Add tool definition in `app/provision.py:TOOLS` list
2. Add `if name == "new_tool_name":` branch in `app/tools.py:ToolExecutor.execute()`
3. Add the `_new_tool()` method on `ToolExecutor`
4. Deploy: `python -m app.provision --deploy` (updates LLM + publishes agent in one step)
5. Update this file's "Current working state" table
