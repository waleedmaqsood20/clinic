"""
Staff dashboard — token-protected /dashboard (HTML), /api/calls (JSON), /api/kpis (JSON).

Phone numbers are masked to the last 4 digits. Summaries and transcripts are
decrypted for authenticated viewers. Set DASHBOARD_TOKEN in .env before using.
"""
from __future__ import annotations
import hmac
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import crypto, repository, call_tracking


def _auth(request: Request) -> None:
    token = os.getenv("DASHBOARD_TOKEN")
    if not token:
        raise HTTPException(503, "Dashboard not configured — set DASHBOARD_TOKEN")
    sent = (request.headers.get("x-dashboard-token")
            or request.query_params.get("token") or "")
    if not hmac.compare_digest(sent, token):
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

    @router.post("/api/sync-retell")
    async def api_sync_retell(request: Request):
        _auth(request)
        api_key = os.getenv("RETELL_API_KEY")
        if not api_key:
            raise HTTPException(503, "RETELL_API_KEY not set")
        import asyncio
        result = await asyncio.to_thread(
            call_tracking.sync_from_retell_api, session_factory, api_key)
        return JSONResponse(content=result)

    @router.get("/api/kpis")
    async def api_kpis(request: Request):
        _auth(request)
        with session_factory() as session:
            kpis = repository.get_kpis(session)
        return JSONResponse(content=kpis)

    @router.get("/api/calls")
    async def api_calls(request: Request):
        _auth(request)
        with session_factory() as session:
            calls = repository.list_recent_calls(session)
            rows = []
            for c in calls:
                summary = transcript = None
                try:
                    summary = crypto.decrypt(c.summary_enc)
                except Exception:
                    pass
                try:
                    transcript = crypto.decrypt(c.transcript_enc)
                except Exception:
                    pass
                rows.append({
                    "call_id": c.call_id,
                    "phone": _mask_phone(c.phone_enc),
                    "outcome": c.outcome,
                    "duration_seconds": c.duration_seconds,
                    "booked": c.booked,
                    "summary": summary,
                    "transcript": transcript,
                    "recording_ref": c.recording_ref,
                    "cost_usd": c.cost_usd,
                    "ended_at": c.ended_at.isoformat() if c.ended_at else None,
                })
        return JSONResponse(content=rows)

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        _auth(request)
        token = request.query_params.get("token", "")
        html = _DASHBOARD_HTML.replace("__TOKEN__", token)
        return HTMLResponse(content=html)

    return router


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bright Smile Dental — Call Dashboard</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f5f5f7;color:#1d1d1f;font-size:14px}
    header{background:#fff;border-bottom:1px solid #e5e5ea;padding:1.25rem 2rem;
           display:flex;align-items:center;gap:1rem}
    header h1{font-size:1rem;font-weight:600}
    header .sub{font-size:.8rem;color:#6e6e73;margin-top:2px}
    .refresh{margin-left:auto;background:#007aff;color:#fff;border:none;
             border-radius:8px;padding:.45rem .9rem;font-size:.8rem;cursor:pointer}
    .refresh:hover{background:#0066d6}
    .sync-btn{background:#f0f0f0;color:#3a3a3c;border:none;border-radius:8px;
              padding:.45rem .9rem;font-size:.8rem;cursor:pointer;margin-left:.5rem}
    .sync-btn:hover{background:#e0e0e0}
    .sync-btn:disabled{opacity:.5;cursor:not-allowed}
    .wrap{max-width:1200px;margin:0 auto;padding:1.5rem 2rem}
    .kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
              gap:.875rem;margin-bottom:1.25rem}
    .kpi{background:#fff;border-radius:12px;padding:1.1rem 1.25rem;
         box-shadow:0 1px 3px rgba(0,0,0,.06)}
    .kpi-val{font-size:1.9rem;font-weight:700;line-height:1}
    .kpi-lbl{font-size:.7rem;color:#6e6e73;margin-top:.35rem;
             text-transform:uppercase;letter-spacing:.05em}
    .section{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.06);
             margin-bottom:1.25rem;overflow:hidden}
    .section-hdr{padding:1rem 1.5rem;border-bottom:1px solid #f0f0f0;
                 font-size:.7rem;font-weight:600;color:#6e6e73;
                 text-transform:uppercase;letter-spacing:.06em}
    .outcome-grid{display:flex;flex-wrap:wrap;gap:1rem;padding:1rem 1.5rem}
    .ob{display:flex;align-items:center;gap:.6rem}
    .ob-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
    .ob-name{color:#3a3a3c;text-transform:capitalize}
    .ob-n{font-weight:600;margin-left:.25rem}
    .dot-booked{background:#34c759}
    .dot-info_given{background:#007aff}
    .dot-abandoned{background:#ff9500}
    .dot-other{background:#aeaeb2}
    table{width:100%;border-collapse:collapse}
    thead th{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;
             color:#6e6e73;font-weight:500;padding:.7rem 1.5rem;
             background:#fafafa;border-bottom:1px solid #f0f0f0;text-align:left}
    tbody td{padding:.8rem 1.5rem;border-bottom:1px solid #f5f5f7;vertical-align:top}
    tbody tr:last-child td{border-bottom:none}
    tbody tr:hover td{background:#fafafa}
    .badge{display:inline-block;font-size:.7rem;padding:.2rem .5rem;
           border-radius:4px;font-weight:500;white-space:nowrap}
    .b-booked{background:#d1fae5;color:#065f46}
    .b-info_given{background:#dbeafe;color:#1e40af}
    .b-abandoned{background:#fef3c7;color:#92400e}
    .b-other{background:#f0f0f0;color:#6e6e73}
    .summary-wrap{max-width:360px}
    .no-sum{color:#aeaeb2;font-style:italic}
    .tx-btn{background:none;border:none;color:#007aff;cursor:pointer;
            font-size:.75rem;padding:0;margin-top:.3rem;display:block}
    .tx-box{display:none;margin-top:.4rem;background:#f5f5f7;border-radius:6px;
            padding:.65rem;font-size:.75rem;line-height:1.65;max-height:180px;
            overflow-y:auto;white-space:pre-wrap;color:#3a3a3c}
    .loading{padding:2.5rem;text-align:center;color:#6e6e73}
    .empty{padding:2.5rem;text-align:center;color:#aeaeb2}
  </style>
</head>
<body>
<header>
  <div>
    <h1>Bright Smile Dental</h1>
    <div class="sub">Voice AI — Call Dashboard</div>
  </div>
  <button class="refresh" onclick="load()">Refresh</button>
  <button class="sync-btn" id="sync-btn" onclick="syncRetell()">Sync History</button>
</header>

<div class="wrap">
  <div class="kpi-grid" id="kpis"><div class="kpi"><div class="loading">Loading…</div></div></div>

  <div class="section" id="outcomes-section" style="display:none">
    <div class="section-hdr">Outcome Breakdown</div>
    <div class="outcome-grid" id="outcomes"></div>
  </div>

  <div class="section">
    <div class="section-hdr">Recent Calls (last 100)</div>
    <div id="calls"><div class="loading">Loading…</div></div>
  </div>
</div>

<script>
const TOKEN = '__TOKEN__';

function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function dur(s){if(!s)return'—';const m=Math.floor(s/60),r=s%60;return m?`${m}m ${r}s`:`${r}s`}
function ts(iso){if(!iso)return'—';return new Date(iso).toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
function badge(o){const m={booked:'booked',info_given:'info_given',abandoned:'abandoned'};const c=m[o]||'other';return`<span class="badge b-${c}">${esc((o||'unknown').replace('_',' '))}</span>`}
function dotcls(o){const m={booked:'booked',info_given:'info_given',abandoned:'abandoned'};return m[o]||'other'}
function toggle(id){const e=document.getElementById(id);e.style.display=e.style.display==='block'?'none':'block'}

async function load(){
  // KPIs
  try{
    const d=await fetch(`/api/kpis?token=${TOKEN}`).then(r=>r.json());
    const rate=d.booking_rate_pct!=null?d.booking_rate_pct+'%':'—';
    document.getElementById('kpis').innerHTML=`
      <div class="kpi"><div class="kpi-val">${esc(d.calls_today)}</div><div class="kpi-lbl">Calls Today</div></div>
      <div class="kpi"><div class="kpi-val">${esc(d.calls_this_week)}</div><div class="kpi-lbl">This Week</div></div>
      <div class="kpi"><div class="kpi-val">${esc(d.total_calls)}</div><div class="kpi-lbl">All Time</div></div>
      <div class="kpi"><div class="kpi-val">${rate}</div><div class="kpi-lbl">Booking Rate</div></div>
      <div class="kpi"><div class="kpi-val">${dur(d.avg_duration_seconds)}</div><div class="kpi-lbl">Avg Duration</div></div>
    `;
    const ob=d.outcome_breakdown||{};
    if(Object.keys(ob).length){
      const total=Object.values(ob).reduce((a,b)=>a+b,0);
      let html='';
      for(const[k,v]of Object.entries(ob).sort((a,b)=>b[1]-a[1])){
        html+=`<div class="ob"><div class="ob-dot dot-${dotcls(k)}"></div><span class="ob-name">${esc(k.replace('_',' '))}</span><span class="ob-n">${v}</span></div>`;
      }
      document.getElementById('outcomes').innerHTML=html;
      document.getElementById('outcomes-section').style.display='block';
    }
  }catch(e){console.error('kpi',e)}

  // Call log
  try{
    const rows=await fetch(`/api/calls?token=${TOKEN}`).then(r=>r.json());
    if(!rows.length){document.getElementById('calls').innerHTML='<div class="empty">No calls recorded yet.</div>';return}
    let t=`<table><thead><tr><th>Time</th><th>Phone</th><th>Duration</th><th>Outcome</th><th>Summary</th></tr></thead><tbody>`;
    rows.forEach((r,i)=>{
      const hasTx=r.transcript&&r.transcript.trim();
      t+=`<tr>
        <td style="white-space:nowrap">${ts(r.ended_at)}</td>
        <td>${esc(r.phone)}</td>
        <td style="white-space:nowrap">${dur(r.duration_seconds)}</td>
        <td>${badge(r.outcome)}</td>
        <td class="summary-wrap">${r.summary?esc(r.summary):'<span class="no-sum">no summary</span>'}${hasTx?`<button class="tx-btn" onclick="toggle('tx${i}')">transcript ▾</button><div class="tx-box" id="tx${i}">${esc(r.transcript)}</div>`:''}</td>
      </tr>`;
    });
    t+='</tbody></table>';
    document.getElementById('calls').innerHTML=t;
  }catch(e){document.getElementById('calls').innerHTML='<div class="empty">Failed to load.</div>'}
}

async function syncRetell(){
  const btn=document.getElementById('sync-btn');
  btn.disabled=true;btn.textContent='Syncing…';
  try{
    const d=await fetch(`/api/sync-retell?token=${TOKEN}`,{method:'POST'}).then(r=>r.json());
    btn.textContent=`Done — ${d.synced} synced`;
    setTimeout(()=>{btn.textContent='Sync History';btn.disabled=false},4000);
    load();
  }catch(e){
    btn.textContent='Failed';
    setTimeout(()=>{btn.textContent='Sync History';btn.disabled=false},3000);
  }
}

load();
</script>
</body>
</html>"""
