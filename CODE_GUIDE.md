# The Complete Guide to Your Clinic Voice AI (Stage 1 · Retell + GoHighLevel)

**What this document is:** a friendly, plain-English tour of the whole project. It
explains what each file does, walks through the important pieces of code, shows how
the parts talk to each other, and gives you exact commands to run, test, change, and
fix things. It's technical, but written so you don't need to be a developer to follow.

> This build uses **Retell AI** for the phone/voice/brain and **GoHighLevel (GHL)**
> for the calendar + contacts (CRM). Read top to bottom the first time; after that,
> use the table of contents.

---

## Table of contents

1. The big picture (60-second mental model)
2. A few words explained simply
3. The folder map — what every file is
4. How a real phone call flows through the code, step by step
5. Every file explained in detail (with the key code blocks)
6. How to run it — exact steps
7. How to change common things (recipes)
8. Every setting (`.env`) explained
9. How to look inside the database
10. Troubleshooting — symptom → cause → fix
11. Safety & security rules (do / don't)
12. Glossary

---

## 1. The big picture (60-second mental model)

Think of the system as **two halves**:

- **Retell** is the *phone line + ears + mouth + brain*. It answers the call, turns
  speech into text, runs the AI, turns text back into speech, and talks to the caller.
  You don't host this — Retell runs it.
- **Your code** is the *clinic's back office*. When the AI needs to actually *do*
  something — look up a price, check the GHL calendar, book an appointment, log the
  call — it calls your back office. Your code does the task and replies, and the AI
  speaks the answer.

```
   Caller ──► Retell (answers, listens, thinks, talks)
                 │   "check the diary / book this"     │  "the call ended / was analyzed"
                 ▼                                      ▼
        POST /retell/function                  POST /retell/webhook
                 │                                      │
                 ▼                                      ▼
            YOUR CODE  ──►  GoHighLevel (calendar + contact)  +  Database (calls, audit)
```

Two doors connect Retell to your code:

- **`/retell/function`** — the AI's *custom-function* calls (FAQ, availability, booking).
- **`/retell/webhook`** — *call events* (`call_started`, `call_ended`, `call_analyzed`)
  so you can record every call.

That's the entire system you run. Everything in `app/` exists to handle those two
doors safely.

---

## 2. A few words explained simply

- **Custom function (Retell's name for a "tool")** — a named action the AI can ask for,
  like `book_appointment`. When the AI uses one, Retell sends your `/retell/function`
  door the function name + the details. Your code runs it and returns one short result.
  **Retell sends one function at a time** (unlike some platforms that batch them).

- **Webhook** — a web address another service calls when something happens. Retell calls
  your `/retell/webhook` when a call starts, ends, or is analyzed.

- **`X-Retell-Signature`** — a stamp Retell puts on every request, made from your Retell
  API key. We check it so only Retell can use your doors.

- **GoHighLevel (GHL)** — your calendar + CRM. We ask it for open times and book into it
  over its API. Booking also creates/updates the **contact** in GHL, so the patient lands
  in your CRM automatically.

- **Private Integration token** — GHL's API key for a single sub-account (location). We
  send it as `Authorization: Bearer <token>` plus a `Version: 2021-07-28` header.

- **Environment variable / `.env`** — settings kept outside the code (keys, IDs, the DB
  address). Code reads them with `os.getenv("NAME")`. Keeps secrets out of the code.

- **Database / ORM** — where information that must survive the call is stored (who booked,
  what happened on each call). We describe tables as Python classes (SQLAlchemy).
  **SQLite** (one file) for testing, **PostgreSQL** in production.

- **Encryption at rest** — scrambling sensitive data before saving (AES-256-GCM). A
  stolen database file would show gibberish.

- **Hashing a phone number** — a one-way fingerprint so we can *match* a caller without
  storing their actual number in plain text.

- **Background task** — work done *after* replying, so the caller isn't kept waiting.
  Used to save the call record after the event arrives.

---

## 3. The folder map — what every file is

```
clinic-voice-agent/
├── START_HERE.md          First-time setup walkthrough (the simplest path)
├── STAGE1.md              What Stage 1 added + go-live + compliance checklist
├── PRODUCTION_SOW.md      The full production plan (all 3 stages)
├── Clinic_Voice_AI_SOW.docx  Same plan, Word version
├── CODE_GUIDE.md          ← THIS document
│
├── app/                   ← all the code that runs
│   ├── knowledge.py       YOUR clinic's facts + FAQ matcher  (the file you edit)
│   ├── provision.py       Creates the Retell LLM + agent (with the custom functions)
│   ├── server.py          The web server — the two doors Retell calls
│   ├── security.py        Verifies each request really came from Retell
│   ├── tools.py           The actions: answer FAQ, check diary, book
│   ├── providers.py       Talks to GoHighLevel (calendar + contacts) and SMS
│   ├── crypto.py          Encrypts sensitive data; hashes phone numbers
│   ├── db.py              Connects to the database
│   ├── models.py          Defines the tables: calls, appointments, audit_log
│   ├── repository.py      Saves/reads rows (the "filing clerk")
│   ├── call_tracking.py   Saves the call record from Retell's call events
│   └── dashboard.py       Staff web page showing recent calls
│
├── tests/
│   ├── test_offline.py    Pretend Retell call, no accounts, no installs
│   └── test_stage1.py     Drives the real app + checks GHL request-building
│
├── Dockerfile             Recipe to package the app for hosting
├── render.yaml            One-click-ish deploy config (fixed public URL)
├── requirements.txt       The Python libraries the app needs
├── .env.example           Template for your secret settings (copy to .env)
├── test.sh                Runs both test suites
├── run.sh                 Installs deps + starts the server
└── create-agent.sh        Creates the Retell LLM + agent
```

Rule of thumb: **`knowledge.py` is the only file you usually edit; everything else is
the machinery.**

---

## 4. How a real phone call flows through the code, step by step

1. **Caller dials your Retell number.** Retell answers with the agent created by
   `provision.py` and plays the greeting (which includes the recording notice).

2. **Caller asks something**, e.g. "How much is a cleaning?" The AI decides to use the
   `lookup_faq` custom function and sends it to your **`/retell/function`** door →
   `server.py: retell_function()`.

3. **Security check.** `server.py` reads the raw body and calls
   `security.py: verify_retell_request()` to confirm the `X-Retell-Signature` matches
   your Retell API key. If not → 401, stop.

4. **Run the action.** `server.py` passes the body to
   `tools.py: handle_function_call()`, which reads `{name, args, call}`, pulls the
   caller's number (`call.from_number`) and call id, and runs `ToolExecutor.execute(...)`.
   For `lookup_faq` it searches `knowledge.py` and returns one sentence.

5. **AI speaks the answer.** Your door returns the result string (as JSON); Retell hands
   it to the agent, which says it out loud.

6. **Caller books.** "Tuesday at 10, I'm Sarah, my tooth hurts." The AI calls
   `check_availability` then `book_appointment`. For booking, `tools.py: _book()`:
   - asks `providers.py` (GHL) for open slots and books the chosen one — which first
     **upserts the contact** in GHL, then **creates the appointment**,
   - **saves the appointment** to your database via `repository.py: record_booking()`,
     encrypting name/phone/reason with `crypto.py`,
   - writes an **audit** entry.

7. **Caller hangs up.** Retell sends `call_ended` (and shortly after, `call_analyzed`
   with a summary) to **`/retell/webhook`** → `server.py: retell_webhook()`, which
   schedules `call_tracking.py: persist_from_retell()` as a **background task**. That
   saves the **call record**: outcome, encrypted summary + transcript, recording link,
   duration, cost.

8. **Staff look later.** A team member opens `/dashboard` (served by `dashboard.py`),
   enters the dashboard token, and sees every call — phone masked to the last 4 digits,
   outcome, and summary.

---

## 5. Every file explained in detail

### 5.1 `app/knowledge.py` — your clinic's facts (the file you edit)

Holds the clinic name, timezone, hours, address, and the question-and-answer list the AI
can speak, plus a tiny matcher that picks the best answer.

**Edit the profile:**
```python
CLINIC_PROFILE = {
    "name": "Bright Smile Dental",
    "timezone": "America/Indiana/Indianapolis",   # Indianapolis is Eastern Time
    "hours": "Monday through Friday, 8am to 5pm, closed weekends",
    "address": "8200 North Meridian Street, Indianapolis, Indiana",
    "parking": "Free patient parking in the lot right out front.",
}
```

**Edit the FAQs** — trigger words on the left, the spoken answer on the right:
```python
FAQS = [
    Doc("how much exam cleaning price cost",
        "A new-patient exam is $89 and a cleaning is $120. ..."),
    ...
]
```

`search(query)` just counts shared words and returns the best match. Save the file; the
AI uses it on the next call (restart the server if it's running).

---

### 5.2 `app/provision.py` — creates the Retell LLM + agent

Retell needs **two objects**, and this file builds and creates both:

1. a **Retell LLM** — the prompt + the three custom functions (wired to your
   `/retell/function` URL) + the underlying model.
2. an **agent** — references that LLM, picks a voice, and sets the **call-events
   webhook** (`/retell/webhook`).

**The instructions (the AI's job description):**
```python
SYSTEM = f"""You are the phone receptionist for {CLINIC['name']} ...
- Treat every caller as a new patient. Get their full name and reason for visit.
- For hours/prices/etc., use lookup_faq and answer from what it gives you.
- You are NOT a clinician. Do not give medical advice ...
- This call may be recorded ...
"""
```

**The custom functions (point back to your door):**
```python
def _tool(name, description, properties, required):
    return {"type": "custom", "name": name, "description": description,
            "url": FUNCTION_URL,                    # https://<host>/retell/function
            "speak_during_execution": True, "speak_after_execution": True,
            "parameters": {"type": "object", "properties": properties, "required": required}}
```

**The two payloads:**
```python
build_llm_payload()   -> {"general_prompt": SYSTEM, "general_tools": TOOLS,
                          "model": MODEL, "begin_message": BEGIN_MESSAGE}
build_agent_payload(llm_id) -> {"response_engine": {"type": "retell-llm", "llm_id": llm_id},
                                "voice_id": VOICE_ID, "webhook_url": WEBHOOK_URL, ...}
```

**How to use it:**
- Preview without an account: `python -m app.provision --dry-run`.
- Create it for real: `bash create-agent.sh` (creates the LLM, then the agent, and prints
  both IDs).
- Then **attach a phone number** to the agent in the Retell dashboard.
- If you change the functions, the prompt, the model, the voice, or your URLs, run
  `bash create-agent.sh` again (it creates a fresh LLM + agent; update the phone number's
  agent if needed).

---

### 5.3 `app/server.py` — the web server (the two doors)

This is the program you run. It receives Retell's requests, checks them, routes them, and
also serves the staff dashboard and a health check.

**Wiring (runs once at startup):**
```python
engine = dbmod.make_engine(DATABASE_URL)         # SQLite or Postgres
SessionLocal = dbmod.make_session_factory(engine)
dbmod.init_db(engine)                            # create tables if missing
executor = ToolExecutor(_make_calendar(), _make_sms(), session_factory=SessionLocal)
app.include_router(make_dashboard_router(SessionLocal))   # /dashboard + /api/calls
```
`_make_calendar()` uses real **GHL** if its three keys are set, otherwise a pretend
calendar.

**Door 1 — custom functions:**
```python
@app.post("/retell/function")
async def retell_function(request: Request):
    raw = await request.body()
    security.verify_retell_request(raw, request.headers.get("x-retell-signature"))
    result = handle_function_call(json.loads(raw or b"{}"), executor)
    return JSONResponse(content=result)      # Retell hands this string to the agent
```

**Door 2 — call events:**
```python
@app.post("/retell/webhook")
async def retell_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    security.verify_retell_request(raw, request.headers.get("x-retell-signature"))
    body = json.loads(raw or b"{}")
    if body.get("event") in ("call_ended", "call_analyzed"):
        background.add_task(persist_from_retell, SessionLocal, body.get("call") or {},
                            body.get("event") == "call_analyzed")
    return JSONResponse(content={"received": True})
```
Note: it reads the **raw body** (needed for the signature), verifies first, then saves
the call in a **background task** so it replies to Retell instantly.

`/health` returns `{"ok": true}` so your host knows the app is alive.

---

### 5.4 `app/security.py` — the bouncer (Retell)

```python
def verify_retell_request(raw_body, signature):
    api_key = os.getenv("RETELL_API_KEY")
    if not api_key:
        return                                  # dev mode: skip
    from retell import Retell
    if not Retell(api_key=api_key).verify(raw_body.decode("utf-8"), api_key, signature or ""):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
```
It uses Retell's official SDK to check the `X-Retell-Signature` over the **raw** body.
If `RETELL_API_KEY` isn't set (local testing) it skips the check, so you can run without
the SDK. Set the key in production so every request is verified.

---

### 5.5 `app/tools.py` — the actions (the back office's hands)

**`handle_function_call()` — the entry point Retell reaches:**
```python
def handle_function_call(body, executor):
    name = body.get("name")
    args = body.get("args") or {}
    call = body.get("call") or {}
    caller = call.get("from_number") or "+10000000000"
    call_id = call.get("call_id")
    out = executor.execute(name, args, caller, call_id)
    return str(out).replace("\n", " ").strip()   # one clean line for the agent
```
Retell sends **one function** with `{name, args, call}`, so this returns **one result
string** (not a list). The caller's number comes from `call.from_number`.

**`ToolExecutor.execute()` — the switchboard:**
```python
def execute(self, name, args, caller_phone, call_id=None):
    if name == "lookup_faq":         return self._faq(args.get("query",""))
    if name == "check_availability": return self._availability(args["day"], args["service"])
    if name == "book_appointment":   return self._book(... , caller_phone, call_id)
```
One `if` per function. Adding a function = one line here + a method (recipe 7.4).

**`_book()` — the most important action:**
```python
def _book(self, day_str, time_str, name, service, reason, caller_phone, call_id):
    slot = _match_time(self.calendar.availability(_parse_day(day_str), service), time_str)
    if not slot: return "That time isn't available — offer another slot."
    conf = self.calendar.book(slot, name, caller_phone, service)   # GHL: contact + appt
    if self.session_factory:                                       # then save it
        from . import repository
        with self.session_factory() as session:
            repository.record_booking(session, call_id=call_id, caller_phone=caller_phone,
                name=name, service=service, reason=reason,
                start_utc=_slot_start_utc(slot), confirmation=conf)
            repository.write_audit(session, actor="voice_ai",
                action="appointment.created", call_id=call_id, phi=True, detail={...})
            session.commit()
    return f"Booked {service} for {name} on {when}. Confirmation {conf}. ..."
```
Safety design: it books in GHL, then saves to your DB **only if** a database is wired in,
and the whole save is wrapped so a storage hiccup never breaks the live call.

**Date/time helpers:** `_parse_day("tomorrow"/"2026-06-16"/"Tuesday")`,
`_match_time(slots, "10am")`, `_slot_start_utc(slot)` (UTC time for safe storage).

---

### 5.6 `app/providers.py` — GoHighLevel (calendar + contacts) and SMS

All contact with GHL lives here, behind simple shapes so the rest of the code doesn't care.

**Key shapes:**
- `Slot` — one opening: `start` (local time), `iso_utc` (the original ISO string GHL gave,
  used to book).
- `InMemoryCalendar` — a **pretend** calendar (9–5) used when GHL keys aren't set; great
  for testing.
- `GHLCalendar` — the **real** integration:
  ```python
  GHLCalendar(token=..., location_id=..., calendar_id=...,
              timezone="America/Indiana/Indianapolis", slot_minutes=30)
  ```
  - `availability(day, service)` → `GET /calendars/{calendarId}/free-slots` with an
    **epoch-millisecond** date range + `timezone`. GHL returns a map keyed by date
    (`"2026-06-16": {"slots": ["2026-06-16T10:00:00-04:00", ...]}`), which we turn into
    `Slot`s.
  - `book(slot, name, phone, service)` → **two calls**: `POST /contacts/upsert`
    (find/create the patient in the CRM, keyed by phone) to get a `contactId`, then
    `POST /calendars/events/appointments` with `{calendarId, locationId, contactId,
    startTime, endTime, title, appointmentStatus}`.
  - Every request carries `Authorization: Bearer <token>` and `Version: 2021-07-28`.
- `SmsProvider` — does nothing (safe default; GHL can send its own confirmations).
  `TwilioSms` — really sends a text (only if you set Twilio keys).

`GHLCalendar` accepts a `client=` argument (any object with `.get/.post`), which is how the
test injects a fake HTTP client to verify the requests without calling GHL.

**How to use it:** set `GHL_API_TOKEN`, `GHL_LOCATION_ID`, `GHL_CALENDAR_ID` in `.env`;
the server then uses the real GHL automatically.

---

### 5.7 `app/crypto.py` — the safe (encryption + phone hashing)

```python
def encrypt(text):  # nonce + AES-256-GCM ciphertext
def decrypt(blob):  # back to text
def phone_hash(phone):  # one-way keyed fingerprint for matching
```
The key comes from `ENCRYPTION_KEY` (`python -m app.crypto` prints a fresh one). If it's
unset, an **insecure dev key** is used with a warning — fine for local testing, never for
real data. In production-plus you'd have a cloud **KMS** hand out the key (a Stage-1
hardening item in the SOW).

⚠️ Changing `ENCRYPTION_KEY` after data exists means old encrypted data can't be read.

---

### 5.8 `app/db.py` + `app/models.py` — the filing cabinets

**`db.py`** — `make_engine(url)` (SQLite locally, Postgres in prod), `make_session_factory`,
`init_db`. You switch databases just by setting `DATABASE_URL`.

**`models.py`** — three tables:
- **`appointments`** — one row per booking; sensitive fields end in `_enc` (encrypted):
  `caller_name_enc`, `caller_phone_enc`, `reason_enc`, plus `phone_hash`, `service`,
  `start_utc`, `calcom_booking_uid` (now holds the **GHL appointment id**), `call_id`, and
  a `patient_id` column that's empty now and ready for Stage 2.
- **`calls`** — one row per call: `call_id` (Retell's), `phone_hash`, `phone_enc`,
  `duration_seconds`, `outcome`, `booked`, `summary_enc`, `transcript_enc`,
  `recording_ref`, `cost_usd`.
- **`audit_log`** — an unchangeable trail of who accessed/changed what (HIPAA).

For the prototype, tables are created automatically; production uses migrations (Alembic).

---

### 5.9 `app/repository.py` — the filing clerk

The only place that writes/reads rows; it encrypts sensitive fields on the way in.
```python
record_booking(session, *, call_id, caller_phone, name, service, start_utc, confirmation, reason=None)
write_audit(session, *, actor, action, call_id=None, phi=False, detail=None)
booking_exists_for_call(session, call_id) -> bool
upsert_call(session, *, call_id, **fields)
list_recent_calls(session, limit=100)
```

---

### 5.10 `app/call_tracking.py` — logging the call from Retell's events

```python
def persist_from_retell(session_factory, call, analyzed):
    # call_ended gives transcript/duration/disconnection; call_analyzed adds the summary
    with session_factory() as session:
        booked = repository.booking_exists_for_call(session, call.get("call_id"))
        repository.upsert_call(session, call_id=call.get("call_id"),
            phone_hash=crypto.phone_hash(number), phone_enc=crypto.encrypt(number),
            duration_seconds=int((end - start)/1000) if start and end else None,
            outcome="booked" if booked else _outcome(call), booked=booked,
            summary_enc=crypto.encrypt(analysis.get("call_summary")),
            transcript_enc=crypto.encrypt(call.get("transcript")),
            recording_ref=call.get("recording_url"),
            cost_usd=(call.get("call_cost") or {}).get("combined_cost"), ...)
        repository.write_audit(session, actor="voice_ai", action="call.recorded", ...)
        session.commit()
```
It **upserts by `call_id`**, so both `call_ended` and `call_analyzed` update the same row.
Outcome is "booked" if a booking was saved during the call, otherwise derived from the
disconnection reason. Runs automatically as a background task.

---

### 5.11 `app/dashboard.py` — the staff web page

A token-protected page (`/dashboard`) and data feed (`/api/calls`). Phone numbers are
masked to the last 4 digits; summaries are decrypted only for a logged-in viewer.
```python
def _auth(request):
    token = os.getenv("DASHBOARD_TOKEN")
    if not token: raise HTTPException(503, "Dashboard not configured ...")
    sent = request.headers.get("x-dashboard-token") or request.query_params.get("token")
    if sent != token: raise HTTPException(401, "unauthorized")
```
Set `DASHBOARD_TOKEN` and open `https://<host>/dashboard?token=<token>`. This MVP login is
intentionally simple; the SOW upgrades it to proper staff sign-in (SSO + MFA).

---

### 5.12 `tests/` — the proof it works

- **`tests/test_offline.py`** — a pretend Retell call through the tools, **no accounts and
  no installs**. Good first check.
- **`tests/test_stage1.py`** — starts the **real app** in memory and checks: the
  `/retell/function` door books and **encrypts** the row, the `/retell/webhook` door
  records the call from `call_analyzed`, the dashboard shows it masked, **and** the
  `GHLCalendar` builds the right free-slots / contact-upsert / appointment requests
  (using a fake HTTP client).

`bash test.sh` runs both. Green "ALL CHECKS PASSED" twice = healthy.

---

### 5.13 `Dockerfile`, `render.yaml`, scripts, `requirements.txt`, `.env.example`

- **`Dockerfile`** — packages the app into a container; runs as non-root with a health
  check.
- **`render.yaml`** — deploy on Render for a **permanent https URL** (so you stop using
  ngrok). After deploy, point Retell's function URL and agent webhook at it.
- **`requirements.txt`** — `fastapi`, `uvicorn`, `httpx`, `sqlalchemy`, `cryptography`,
  `retell-sdk`.
- **`.env.example`** — a template of every setting. Copy to `.env` and fill in.
- **`test.sh` / `run.sh` / `create-agent.sh`** — the three commands you actually use.

---

## 6. How to run it — exact steps

### 6.1 See it work on your computer (2 minutes, no accounts)
```
cd clinic-voice-agent
bash test.sh
```
Expect two blocks ending in **ALL CHECKS PASSED ✅**.

### 6.2 Run the server on your computer
```
cp .env.example .env
python -m app.crypto           # prints an ENCRYPTION_KEY — paste it into .env
bash run.sh                    # installs libraries the first time, then starts
```
Check it's alive: `http://localhost:4242/health` → `{"ok": true}`.

### 6.3 Make it answer a real phone
1. Fill `.env`: `RETELL_API_KEY`, plus GHL's `GHL_API_TOKEN`, `GHL_LOCATION_ID`,
   `GHL_CALENDAR_ID`, and the security keys (`ENCRYPTION_KEY`, `PHONE_HASH_HMAC_KEY`,
   `DASHBOARD_TOKEN`).
2. Give the server a public address. Quick test option:
   ```
   ngrok http 4242
   ```
   Then set, in `.env`:
   ```
   RETELL_FUNCTION_URL=https://<your-ngrok>.ngrok.app/retell/function
   RETELL_WEBHOOK_URL=https://<your-ngrok>.ngrok.app/retell/webhook
   ```
   (For production, deploy with `render.yaml`/`Dockerfile` to get a permanent URL.)
3. Create the Retell LLM + agent:
   ```
   bash create-agent.sh
   ```
4. In the Retell dashboard, **attach a phone number** to the agent.
5. Call the number. 🎉

### 6.4 Open the staff dashboard
```
https://<your-host>/dashboard?token=<your DASHBOARD_TOKEN>
```
Or fetch raw data: `GET /api/calls` with header `x-dashboard-token: <token>`.

> **Remember:** any time you change functions, the prompt, the model/voice, or your URLs,
> run `bash create-agent.sh` again and re-attach the number to the new agent if needed.

---

## 7. How to change common things (recipes)

### 7.1 Change clinic hours / prices / address
Edit `CLINIC_PROFILE` and the answers in `FAQS` in `app/knowledge.py`. Restart the server.

### 7.2 Add a new FAQ
Add one line to `FAQS`:
```python
Doc("sedation nervous anxiety calm",
    "Yes, we offer sedation options for nervous patients — the dentist will discuss what's right for you."),
```

### 7.3 Use a different GHL calendar or sub-account
Change `GHL_CALENDAR_ID` (and `GHL_LOCATION_ID` for a different sub-account) in `.env`.
Restart. If your appointment length differs, set `GHL_SLOT_MINUTES`.

### 7.4 Add a brand-new custom function (example: "leave a callback request")
1. **Describe it to the AI** in `app/provision.py` `TOOLS`:
   ```python
   _tool("request_callback",
         "Take a message for the team to call the patient back.",
         {"name": {"type":"string"}, "note": {"type":"string"}},
         ["name","note"]),
   ```
2. **Handle it** in `app/tools.py` `ToolExecutor.execute`:
   ```python
   if name == "request_callback":
       return self._callback(args.get("name",""), args.get("note",""), caller_phone, call_id)
   ```
   and add a `_callback(...)` method that saves it (reuse `repository.write_audit` or add a
   small table).
3. **Re-create the agent:** `bash create-agent.sh`.

### 7.5 Change the AI's voice or model
In `.env`: set `RETELL_MODEL` (a current Retell-supported model string) and
`RETELL_VOICE_ID` (a Retell voice id). Re-run `bash create-agent.sh`.

### 7.6 Switch from the test database (SQLite) to production (Postgres)
Set `DATABASE_URL` to your Postgres connection string. Restart. Nothing else changes.

### 7.7 Generate / rotate secrets
- Encryption key: `python -m app.crypto` → put in `ENCRYPTION_KEY`.
- Other secrets (`PHONE_HASH_HMAC_KEY`, `DASHBOARD_TOKEN`): any long random string.
- ⚠️ Rotating `ENCRYPTION_KEY` or `PHONE_HASH_HMAC_KEY` after real data exists needs a
  planned re-encryption step. For Stage 1 testing it's fine (no real data yet).
- Rotate your **GHL Private Integration token** every ~90 days (GHL gives a 7-day overlap).

---

## 8. Every setting (`.env`) explained

| Setting | Required? | What it does |
|---|---|---|
| `RETELL_API_KEY` | yes | Creates the agent **and** verifies incoming Retell signatures |
| `RETELL_FUNCTION_URL` | to go live | Your public URL + `/retell/function`; baked into the custom functions |
| `RETELL_WEBHOOK_URL` | to go live | Your public URL + `/retell/webhook`; set as the agent's call-events webhook |
| `GHL_API_TOKEN` | for real bookings | GHL Private Integration token (Bearer) |
| `GHL_LOCATION_ID` | for real bookings | The GHL sub-account (location) id |
| `GHL_CALENDAR_ID` | for real bookings | Which GHL calendar to book into |
| `ENCRYPTION_KEY` | before go-live | Encrypts stored PHI (`python -m app.crypto`) |
| `PHONE_HASH_HMAC_KEY` | before go-live | Secret used to fingerprint phone numbers |
| `DASHBOARD_TOKEN` | to use dashboard | Password to open `/dashboard` |
| `DATABASE_URL` | optional | `sqlite:///./clinic.db` for dev; a Postgres URL for prod |
| `CLINIC_TZ` | optional | Timezone (default `America/Indiana/Indianapolis`) |
| `RETELL_MODEL` | optional | Which model the Retell LLM uses |
| `RETELL_VOICE_ID` | optional | The agent's voice |
| `GHL_SLOT_MINUTES` | optional | Appointment length sent to GHL (default 30) |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` | optional | Only if YOU send SMS instead of GHL |

---

## 9. How to look inside the database

If you're using the default SQLite file (`clinic.db`):
```
sqlite3 clinic.db ".tables"
sqlite3 clinic.db "SELECT call_id, outcome, booked, duration_seconds FROM calls;"
sqlite3 clinic.db "SELECT service, start_utc, status FROM appointments;"
sqlite3 clinic.db "SELECT occurred_at, actor, action FROM audit_log ORDER BY id DESC LIMIT 10;"
```
Columns ending in `_enc` look like random bytes — that's the encryption working. Use the
dashboard to read them decrypted as an authenticated viewer.

---

## 10. Troubleshooting — symptom → cause → fix

| Symptom | Likely cause | Fix |
|---|---|---|
| `bash test.sh` errors immediately | Not in the project folder, or no Python | `cd` into `clinic-voice-agent`; install Python 3.10+ |
| `/health` fails | Server not running / wrong port | Check the `run.sh` window; confirm port 4242 |
| Retell function call returns **401** | Signature mismatch | Make sure `RETELL_API_KEY` in `.env` is the same key the agent belongs to |
| AI never calls your function | Function URL wrong, or agent not re-created | Re-run `bash create-agent.sh` with the right `RETELL_FUNCTION_URL`; re-attach the number |
| No call records appear | Agent webhook not set | Ensure `RETELL_WEBHOOK_URL` is set and the agent has it; re-create the agent |
| GHL booking fails | Wrong token/location/calendar, or missing scopes | Check the three GHL values; the token needs calendars + contacts write scopes |
| GHL returns no slots | Calendar has no availability, or wrong calendar id | Confirm `GHL_CALENDAR_ID` and that the calendar has open hours |
| Dashboard **503** | `DASHBOARD_TOKEN` not set | Set it and restart |
| Dashboard **401** | Wrong/missing token | Use `?token=<DASHBOARD_TOKEN>` or the `x-dashboard-token` header |
| "ENCRYPTION_KEY not set" warning | No key in `.env` | Fine for local testing; **set a real key before real calls** |

---

## 11. Safety & security rules (do / don't)

**Do**
- Set real values for `RETELL_API_KEY`, `ENCRYPTION_KEY`, `PHONE_HASH_HMAC_KEY`, and
  `DASHBOARD_TOKEN` before any real patient call.
- Keep `.env` **out of version control**.
- Use Postgres + a managed host in production; keep everything in a US region.
- Before real PHI: sign **BAAs** with **Retell** and **GoHighLevel** (GHL's HIPAA
  features are a paid add-on — confirm it's enabled on your account), and complete the
  **HIPAA Security Risk Analysis** (see `STAGE1.md`).

**Don't**
- Don't let the AI give medical/dental advice (the instructions already forbid this).
- Don't log full transcripts or full phone numbers in plain text (the code avoids this).
- Don't change `ENCRYPTION_KEY` once real data exists without a re-encryption plan.
- Don't rely on the simple dashboard token for real staff access long-term — upgrade to
  proper sign-in (in the SOW).

---

## 12. Glossary

- **Retell** — the service that runs the phone call, speech, and AI.
- **Custom function** — Retell's term for a tool the AI can call (e.g., `book_appointment`).
- **`/retell/function` / `/retell/webhook`** — your two doors: actions, and call events.
- **`X-Retell-Signature`** — the stamp proving a request came from Retell.
- **GoHighLevel (GHL)** — your calendar + CRM; bookings also create the contact.
- **Private Integration token** — GHL's API key for one sub-account.
- **PHI** — protected health information (patient data); must be handled carefully.
- **Env var / `.env`** — settings/secrets kept outside the code.
- **ORM / SQLAlchemy** — describing database tables as Python classes.
- **SQLite / PostgreSQL** — a file-based DB (dev) / a server DB (production).
- **AES-256-GCM** — strong encryption used to protect stored data.
- **Hash (HMAC)** — a one-way fingerprint; used to match phone numbers privately.
- **BAA** — Business Associate Agreement, a HIPAA contract with each vendor.
- **Background task** — work done after replying, so the caller isn't kept waiting.
