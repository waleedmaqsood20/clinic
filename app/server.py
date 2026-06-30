"""
The only server you run (Stage 1) — Retell + GoHighLevel.
Build: 2026-06-30-r4  (POST contacts/search, GET calendars/events, cancel via PUT)

  uvicorn app.server:app --port 4242   (run.sh does this for you)

Retell handles the phone call; this server exposes two endpoints:
  - POST /retell/function : the agent's custom-function calls (FAQ, availability,
    booking). We run the action and return one result string.
  - POST /retell/webhook  : call events (call_started / call_ended / call_analyzed).
    We record each call.

Both verify Retell's signature. With GHL keys it books into your GHL calendar and
creates the contact in your CRM; without them it uses a pretend calendar. With no
DATABASE_URL it stores data in a local SQLite file.
"""
from __future__ import annotations
import os
import json
import logging

logger = logging.getLogger("clinic")

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from .tools import ToolExecutor, handle_function_call
from .providers import GHLCalendar, InMemoryCalendar, SmsProvider, TwilioSms
from . import db as dbmod, security
from .call_tracking import persist_from_retell
from .dashboard import make_dashboard_router

CLINIC_TZ = os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./clinic.db")

engine = dbmod.make_engine(DATABASE_URL)
SessionLocal = dbmod.make_session_factory(engine)
dbmod.init_db(engine)

app = FastAPI(title="Clinic Voice AI — Stage 1 (Retell + GHL)")
logger.info("=== Clinic Voice AI starting — build 2026-06-30-r4 ===")


def _make_calendar():
    token = os.getenv("GHL_API_TOKEN")
    location = os.getenv("GHL_LOCATION_ID")
    calendar = os.getenv("GHL_CALENDAR_ID")
    if token and location and calendar:
        return GHLCalendar(token=token, location_id=location, calendar_id=calendar,
                           timezone=CLINIC_TZ,
                           slot_minutes=int(os.getenv("GHL_SLOT_MINUTES", "30")))
    return InMemoryCalendar()          # pretend calendar for first tests


def _make_sms():
    if os.getenv("TWILIO_ACCOUNT_SID"):
        return TwilioSms(os.environ["TWILIO_ACCOUNT_SID"],
                         os.environ["TWILIO_AUTH_TOKEN"],
                         os.environ["TWILIO_FROM_NUMBER"])
    return SmsProvider()               # no-op; GHL can send its own confirmations


executor = ToolExecutor(_make_calendar(), _make_sms(), session_factory=SessionLocal)
app.include_router(make_dashboard_router(SessionLocal))


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/retell/function")
async def retell_function(request: Request):
    raw = await request.body()
    security.verify_retell_request(raw, request.headers.get("x-retell-signature"))
    body = json.loads(raw or b"{}")
    fn_name = body.get("name", "?")
    has_args = body.get("args") is not None
    logger.info("[RETELL] fn=%s has_args=%s keys=%s", fn_name, has_args, list(body.keys()))
    result = handle_function_call(body, executor)
    logger.info("[RETELL] fn=%s -> %s", fn_name, result[:80])
    return JSONResponse(content={"result": result})


@app.post("/retell/webhook")
async def retell_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    security.verify_retell_request(raw, request.headers.get("x-retell-signature"))
    body = json.loads(raw or b"{}")
    event = body.get("event")
    call = body.get("call") or {}
    if event in ("call_ended", "call_analyzed"):
        background.add_task(persist_from_retell, SessionLocal, call,
                            event == "call_analyzed")
    return JSONResponse(content={"received": True})
