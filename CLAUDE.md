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

Last updated: 2026-06-30

| Feature | Status | Notes |
|---|---|---|
| FAQ lookup | ✅ Working | |
| Check availability | ✅ Working | |
| Book appointment | ✅ Working | GHL upsert + appointment create |
| Cancel appointment | ✅ Code fixed | Needs live test with a FUTURE appointment |
| Reschedule appointment | ⚠️ Not tested | Code in place, never tested end-to-end |
| SMS confirmation | ⏸ Paused | Client setting up Retell text integration |
| Staff dashboard | ✅ Live | `/dashboard?token=<DASHBOARD_TOKEN>` |
| Call tracking | ✅ Working | call_ended + call_analyzed persisted |

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

### Contact lookup: use `/contacts/search`, not `/contacts/search/duplicate`

`/contacts/search/duplicate` is a pre-creation duplicate check — unreliable for finding existing contacts.

Current `_find_contact_by_phone()` tries `duplicate` first, then falls back to:
```
GET /contacts/search?locationId=...&query=<e164_phone>&pageSize=5
```
The fallback is the reliable one.

### Appointment lookup

`GET /contacts/{contactId}/appointments` returns `{ "appointments": [...] }` (or sometimes a bare list). We filter to `startTime > now` in UTC and pick the earliest.

**Timezone caution:** `startTime` in GHL responses may or may not have a timezone offset. We always attach UTC if tzinfo is missing before comparing.

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

---

## Pending work

- [ ] **Live cancel test** — book a future appointment, then call to cancel it and verify GHL deletes it
- [ ] **Reschedule test** — book future appt, call to reschedule, verify GHL updates it
- [ ] **Render upgrade** — move to paid tier to eliminate cold-start sleep
- [ ] **PostgreSQL** — switch from SQLite to managed Postgres for production
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

## How to update the Retell LLM prompt/tools without recreating the agent

```powershell
$env:RETELL_API_KEY="your_key"
$env:RETELL_FUNCTION_URL="https://clinic-xprt.onrender.com/retell/function"
venv\Scripts\python.exe -m app.provision --update-llm llm_9ea568d5a33e1c830c557936ad68
```

This patches the existing LLM in place — the agent and phone number don't need to change.

---

## Adding a new tool (checklist)

1. Add tool definition in `app/provision.py:TOOLS` list
2. Add `if name == "new_tool_name":` branch in `app/tools.py:ToolExecutor.execute()`
3. Add the `_new_tool()` method on `ToolExecutor`
4. Update the Retell LLM: `python -m app.provision --update-llm <llm_id>`
5. Update this file's "Current working state" table
