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

Last updated: 2026-07-13

| Feature | Status | Notes |
|---|---|---|
| FAQ lookup | ✅ Working | |
| Check availability | ✅ Working | |
| Book appointment | ✅ Working | GHL upsert + appointment create |
| Check upcoming appointments | ✅ Working | Returns event_id, date, time, service, booked_name |
| Cancel appointment | ✅ Live tested | Confirmed working with real caller (Alex, Jul 2026) |
| Reschedule appointment | ⚠️ Not tested | Code in place, never tested end-to-end |
| Week availability injection | ⚠️ Agent v6 deployed, not yet confirmed by real call | Inbound webhook → `{{week_availability}}` variable → prompt |
| SMS confirmation | ⏸ Paused | Client setting up Retell text integration |
| Staff dashboard | ✅ Live | `/dashboard?token=<DASHBOARD_TOKEN>` |
| Call tracking | ✅ Working | call_ended + call_analyzed + recording playback |
| Availability prefetch | ✅ Wired | call_started → cache → sub-100ms tool fallback |
| Appointments tab | ✅ Built (Jul 12) | /api/appointments — upcoming bookings from the appointments table |
| Call log filters + pagination + CSV | ✅ Built (Jul 12) | outcome/date filters, 50/page, /api/calls.csv export |
| 14-day trend chart | ✅ Built (Jul 12) | inline SVG, data from /api/kpis daily_trend |
| Needs-attention flags | ✅ Built (Jul 12) | abandoned / <15s / keyword match; server-side in /api/calls |
| Daily digest | ✅ Built (Jul 12) | GET /api/digest preview, POST /api/send-digest; env: DIGEST_TO_NUMBER, optional DIGEST_HOUR scheduler (best-effort on free tier) |
| Dead-letter queue | ✅ Built (Jul 12) | failed_events table; failed webhook persists auto-stored; replay from Issues tab |
| Deep health check | ✅ Built (Jul 12) | GET /health/deep — DB + GHL + Retell; point uptime pinger here (doubles as keep-warm) |
| Recording playback (fresh URLs) | ✅ Built (Jul 12) | /api/recording/{call_id} fetches fresh presigned URL per play |
| Auto-sync scheduler | ✅ Built (Jul 12) | every AUTO_SYNC_HOURS (default 6, 0 disables); needs RETELL_API_KEY |
| Day-before SMS reminders | ✅ Built (Jul 12) | daily at REMINDER_HOUR (default 16 local) + POST /api/send-reminders + dashboard button; idempotent via appointments.reminder_sent |
| Insurance intake | ✅ Built (Jul 12) | 'insurance' arg on book_appointment → encrypted in DB + GHL contact note; prompt step 6b added — REQUIRES `--deploy` to go live on the agent |
| Post-call booking verification | ✅ Built (Jul 12) | call_analyzed → GHL GET appointment; calls.booking_verified; 'unverified' badge + attention flag |
| New-patient tracking | ✅ Built (Jul 12) | computed from phone_hash history (sync-order safe); NEW badges in calls+appointments, monthly chart, KPI card, digest line |
| Schema migration | ✅ Built (Jul 12) | db.init_db runs idempotent ADD COLUMNs — safe on existing Postgres |
| Patient registry | ✅ Built (Jul 13) | patients table keyed by phone_hash; auto-linked on calls+bookings; startup backfill of history (idempotent) |
| Patient profiles | ✅ Built (Jul 13) | Patients tab: search, per-patient call/appointment history, editable encrypted staff notes; full phone shown in profile (audited: patient.profile_viewed) |
| Caller recognition | ✅ Built (Jul 13) | /retell/inbound injects {{caller_context}} (name + next appointment) — REQUIRES `--deploy` (prompt changed) |
| Weekly/monthly reports | ✅ Built (Jul 13) | GET /api/report?period=week|month + POST /api/send-report (SMS to DIGEST_TO_NUMBER); view buttons in Analytics tab |
| Call analytics | ✅ Built (Jul 13) | Analytics tab: hourly/weekday charts, conversion, abandon, after-hours, FAQ-fallback mining from transcripts |
| Revenue attribution | ✅ Built (Jul 13) | SERVICE_PRICES_JSON × bookings; KPI card + by-service table; estimates only |
| No-show tracking | ✅ Built (Jul 13) | sync_appointment_statuses pulls final GHL statuses (piggybacks auto-sync; POST /api/sync-appointment-statuses); no-show rate split by reminder sent |
| Outbound calls | ✅ Built (Jul 13) | outbound.py → Retell create-phone-call; same agent, {{outbound_purpose}}/{{outbound_context}} activate Outbound Mode in prompt; "Call patient" button, POST /api/call-patient/{id}, POST /api/outbound-confirmations. Needs RETELL_PHONE_NUMBER. REQUIRES --deploy |
| Waitlist | ✅ Built (Jul 13) | waitlist table; add_to_waitlist voice tool (prompt: offer when day is full); cancel (voice or SMS) auto-offers freed slot via WAITLIST_NOTIFY=sms|call|off; Waitlist tab with manual offer/remove. REQUIRES --deploy |
| Two-way SMS | ✅ Built (Jul 13) | POST /sms/inbound (set as Twilio Messaging webhook + SMS_WEBHOOK_URL for signature validation); C=confirm, X=cancel (GHL+local+waitlist trigger), R=reschedule flag (+optional outbound call via OUTBOUND_ON_RESCHEDULE=1); reminder text says "Reply C/R/X" |
| Dashboard logins | ✅ Built (Jul 13) | dashboard_users table, PBKDF2 passwords, signed-cookie sessions (12h, DASHBOARD_SECRET), /login page, admin/staff roles, user mgmt in Admin tab, login throttling, full audit. First admin: DASHBOARD_ADMIN_USER/PASSWORD env. Legacy DASHBOARD_TOKEN still works (admin) — rotate it after users exist |
| Patient edit + merge | ✅ Built (Jul 13) | PUT /api/patients/{id} (name/insurance/dob/notes); merge two records (second phone number) with full history relink; editable fields + merge UI in profile |
| Caller context v2 | ✅ Built (Jul 13) | name, visit count, last visit, up to 2 upcoming appts, insurance on file. REQUIRES --deploy |
| Local status sync | ✅ Built (Jul 13) | voice/SMS cancel + reschedule now update OUR appointment row too (was GHL-only) — keeps profiles, no-show stats and waitlist accurate |
| Multi-clinic foundation | ✅ Schema ready (Jul 13) | clinics table + clinic_id on patients/calls/appointments; clinic #1 auto-bootstrapped. REMAINING for clinic #2: per-number inbound routing, per-clinic GHL/Retell creds, per-clinic dashboard tokens, clinic_id filters in queries |

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
| (Jul 9 v6) | `get_week_availability` never fired at call-start; "no response needed" spoken mid-call | Rearchitected: inbound call webhook (`POST /retell/inbound`) injects `{{week_availability}}` as dynamic variable before call connects; `begin_message` restored (instant greeting, no LLM compliance required); silence convention restored to `no response needed` string (Retell suppresses TTS on this exact string); `--deploy` now also sets `inbound_webhook_url` on the phone number |

---

## Pending work

- [x] **Live cancel test** — confirmed working with real caller (Alex, Jul 2026)
- [x] **Deploy automation** — `--deploy` patches LLM + publishes agent + sets phone inbound webhook in one step
- [x] **Week availability architecture** — rearchitected to inbound webhook + dynamic variable (agent v6 deployed Jul 9)
- [ ] **Agent v6 live-call test** — confirm: instant greeting (begin_message), Render logs show `[INBOUND]` hit, asking about any day this week causes zero tool calls, "no response needed" is NOT audible (transcript may log it, call should be silent)
- [ ] **Cold-start test** — let Render sleep 15+ min, call; begin_message restored so greeting is instant even on cold start, but inbound webhook still hits backend → verify it doesn't time out
- [ ] **Slot-taken test** — book a slot manually, call and request it, confirm Sarah recovers with alternatives
- [ ] **Reschedule test** — book future appt, call to reschedule, verify GHL updates it (never tested end-to-end)
- [ ] **Render upgrade** — move to paid tier to eliminate cold-start sleep (real fix for cold-start dead air)
- [ ] **Rotate API keys** — rotate GHL token + Retell key (security hygiene)
- [ ] **SMS** — client setting up Retell text; wire in when ready
- [ ] **Deploy agent v7+** — caller recognition ({{caller_context}}) AND insurance intake both changed the prompt/tools: run `python -m app.provision --deploy` after pushing
- [ ] **(superseded, merged above) Deploy agent v7** — insurance intake (tool arg + prompt 6b) needs `python -m app.provision --deploy` after pushing backend
- [ ] **Twilio creds** — reminders + digest + booking confirmations all no-op silently until TWILIO_* env vars are set
- [ ] **Digest delivery** — set DIGEST_TO_NUMBER (+ Twilio creds or GHL SMS); set DIGEST_HOUR only after Render paid tier, else use external cron on POST /api/send-digest
- [ ] **/api/calls response shape changed (Jul 12)** — now {rows, total, offset, limit}; test_stage1.py updated accordingly
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
venv\Scripts\python.exe -m app.provision --deploy
```

`--deploy` does four things in one command:
1. PATCHes the LLM (new LLM version created)
2. Finds the auto-created draft agent version Retell generates after the PATCH
3. Publishes that draft — making it live for real calls immediately
4. Sets `inbound_webhook_url` on the phone number (from `RETELL_PHONE_NUMBER` in `.env`)

**Also push code to GitHub** after any changes to `server.py` or `tools.py` — `--deploy` only updates the Retell LLM/agent prompt, not the backend code running on Render.

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
