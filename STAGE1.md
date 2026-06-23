# Stage 1 — what was built (Retell + GoHighLevel)

This is the first production stage from the SOW. **Every caller is treated as a new
patient** (no patient database / no identity verification yet — that's Stage 2).
It runs live on **Retell**, books into **GoHighLevel** (and creates the contact in your
CRM), tracks every call, and meets the HIPAA baseline.

## What Stage 1 adds on top of the prototype

| Area | What's new | Where |
|------|-----------|-------|
| Retell agent | Retell LLM + agent with three custom functions + recording notice | `app/provision.py` |
| Two webhooks | `/retell/function` (actions) and `/retell/webhook` (call events) | `app/server.py` |
| New-patient capture | Collects name + reason; phone from caller ID | `app/tools.py`, `app/provision.py` |
| GHL calendar + CRM | Free-slots lookup, contact upsert, appointment create | `app/providers.py` |
| Booking persistence | Each booking written to the DB (encrypted) with an audit entry | `app/repository.py`, `app/models.py` |
| Call tracking | `call_ended` / `call_analyzed` stored: outcome, summary, transcript, recording, cost | `app/call_tracking.py` |
| Encryption at rest | PHI fields AES-256-GCM encrypted | `app/crypto.py` |
| Webhook auth | Retell `X-Retell-Signature` verified on every request | `app/security.py` |
| Staff dashboard | Token-gated `/dashboard` + `/api/calls`; phone masked to last 4 | `app/dashboard.py` |
| Database | SQLite for dev, PostgreSQL in prod via `DATABASE_URL` | `app/db.py` |
| Deployment | `Dockerfile` + `render.yaml` for a fixed HTTPS URL (no ngrok) | repo root |
| Indiana/US | Eastern Time, USD pricing, US insurance wording | `app/knowledge.py` |

## Try it on your computer (no accounts)

```
bash test.sh
```
Runs two suites and should print **ALL CHECKS PASSED** twice. The second suite spins up
the real app and verifies booking persistence, encryption, webhook auth, call tracking,
the dashboard, **and** that the GHL provider builds the correct free-slots / contact /
appointment requests — all without calling any external service.

## Go live (Stage 1)

1. Generate your secrets:
   ```
   python -m app.crypto            # prints an ENCRYPTION_KEY
   ```
   Put it (and random strings for `PHONE_HASH_HMAC_KEY`, `DASHBOARD_TOKEN`) into `.env`
   (copy from `.env.example`).
2. Deploy the server somewhere with a fixed URL (Render/Railway/Fly — `Dockerfile` and
   `render.yaml` included) and attach a managed **PostgreSQL**; set `DATABASE_URL`.
   Set `RETELL_FUNCTION_URL` and `RETELL_WEBHOOK_URL` to your `https://<host>/retell/...`.
3. Add your GHL keys: `GHL_API_TOKEN` (Private Integration), `GHL_LOCATION_ID`,
   `GHL_CALENDAR_ID`.
4. Create the Retell LLM + agent:
   ```
   bash create-agent.sh
   ```
5. In Retell, **attach a phone number** to the agent. Call it.
6. Open the dashboard: `https://<host>/dashboard?token=<DASHBOARD_TOKEN>`.

> Re-running `create-agent.sh` creates a fresh LLM + agent. After doing so, re-attach the
> phone number to the new agent (or update the agent in place from the Retell dashboard).

## Compliance note (must-do before real calls)

This handles PHI, so before going live with real patients: execute **BAAs** with
**Retell** and **GoHighLevel** (GHL's HIPAA features are a paid add-on — confirm it's
enabled), complete the HIPAA **Security Risk Analysis**, and keep `ENCRYPTION_KEY` and all
secrets in a real secrets manager (not committed). Production should also wrap the data key
with a cloud **KMS** and move transcripts/recordings to an encrypted object store. See
`PRODUCTION_SOW.md` / the Word SOW for the full checklist.

## What's intentionally NOT in Stage 1

Patient identification by caller ID, identity verification (KBA), and speaking a patient's
medical record — all arrive in **Stage 2**. The data layer and call records built here are
designed so Stage 2 slots in cleanly (e.g., `calls.patient_id` already exists).
