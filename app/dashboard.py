"""
Staff dashboard — token-protected /dashboard (HTML) and /api/calls (JSON).

Phone numbers are masked to the last 4 digits. Summaries are decrypted for
authenticated viewers. Set DASHBOARD_TOKEN in .env before using.
"""
from __future__ import annotations
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import crypto, repository


def _auth(request: Request) -> None:
    token = os.getenv("DASHBOARD_TOKEN")
    if not token:
        raise HTTPException(503, "Dashboard not configured — set DASHBOARD_TOKEN")
    sent = (request.headers.get("x-dashboard-token")
            or request.query_params.get("token"))
    if sent != token:
        raise HTTPException(401, "unauthorized")


def _mask_phone(enc: bytes | None) -> str:
    if not enc:
        return "****"
    try:
        phone = crypto.decrypt(enc) or ""
        return ("****" + phone[-4:]) if len(phone) >= 4 else "****"
    except Exception:
        return "****"


def make_dashboard_router(session_factory) -> APIRouter:
    router = APIRouter()

    @router.get("/api/calls")
    async def api_calls(request: Request):
        _auth(request)
        with session_factory() as session:
            calls = repository.list_recent_calls(session)
            rows = []
            for c in calls:
                summary = None
                try:
                    summary = crypto.decrypt(c.summary_enc)
                except Exception:
                    pass
                rows.append({
                    "call_id": c.call_id,
                    "phone": _mask_phone(c.phone_enc),
                    "outcome": c.outcome,
                    "duration_seconds": c.duration_seconds,
                    "booked": c.booked,
                    "summary": summary,
                    "recording_ref": c.recording_ref,
                    "cost_usd": c.cost_usd,
                    "ended_at": c.ended_at.isoformat() if c.ended_at else None,
                })
        return JSONResponse(content=rows)

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        _auth(request)
        token = request.query_params.get("token", "")
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Clinic Call Dashboard</title>
  <style>
    body {{ font-family: sans-serif; padding: 2em; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ccc; padding: .5em; text-align: left; }}
    th {{ background: #f0f0f0; }}
  </style>
</head>
<body>
<h1>Recent Calls</h1>
<div id="data">Loading…</div>
<script>
fetch('/api/calls?token={token}')
  .then(r => r.json())
  .then(rows => {{
    if (!rows.length) {{
      document.getElementById('data').textContent = 'No calls yet.';
      return;
    }}
    let t = '<table><tr><th>Call ID</th><th>Phone</th><th>Outcome</th>'
          + '<th>Duration (s)</th><th>Booked</th><th>Summary</th></tr>';
    rows.forEach(r => {{
      t += `<tr>
        <td>${{r.call_id || ''}}</td>
        <td>${{r.phone || ''}}</td>
        <td>${{r.outcome || ''}}</td>
        <td>${{r.duration_seconds ?? ''}}</td>
        <td>${{r.booked ? 'Yes' : 'No'}}</td>
        <td>${{r.summary || ''}}</td>
      </tr>`;
    }});
    t += '</table>';
    document.getElementById('data').innerHTML = t;
  }});
</script>
</body>
</html>"""
        return HTMLResponse(content=html)

    return router
