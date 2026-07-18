"""
Analytics, revenue attribution, and the weekly/monthly report.

Revenue is an ESTIMATE: appointments × configured price per service.
Configure with SERVICE_PRICES_JSON, e.g.:
  SERVICE_PRICES_JSON={"exam": 89, "cleaning": 120, "whitening": 199, "default": 100}
Defaults mirror the FAQ price list in knowledge.py.
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
from zoneinfo import ZoneInfo

from . import crypto, repository
from .models import Appointment, Call

logger = logging.getLogger("clinic")

_DEFAULT_PRICES = {"exam": 89, "cleaning": 120, "whitening": 199,
                   "filling": 250, "default": 100}

# Phrases the agent says when the FAQ has no answer — counted as
# "unanswered questions" so the clinic knows what to add to the FAQ.
_FALLBACK_PHRASES = ("i don't have that to hand",
                     "i'm having a little trouble",
                     "someone from our team follows up")

_REVENUE_EXCLUDED_STATUSES = ("cancelled", "invalid", "noshow")


def service_prices() -> dict:
    raw = os.getenv("SERVICE_PRICES_JSON")
    if not raw:
        return dict(_DEFAULT_PRICES)
    try:
        prices = {str(k).lower(): float(v) for k, v in json.loads(raw).items()}
        prices.setdefault("default", _DEFAULT_PRICES["default"])
        return prices
    except Exception:
        logger.exception("[REVENUE] bad SERVICE_PRICES_JSON — using defaults")
        return dict(_DEFAULT_PRICES)


def _price_for(service: str | None, prices: dict) -> float:
    s = (service or "").lower().strip()
    for key, price in prices.items():
        if key != "default" and key in s:
            return price
    return prices.get("default", 0)


def _tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis"))


def _aware(d: dt.datetime | None) -> dt.datetime | None:
    if d is not None and d.tzinfo is None:
        return d.replace(tzinfo=dt.timezone.utc)
    return d


def revenue_stats(session, start: dt.datetime | None = None,
                  end: dt.datetime | None = None) -> dict:
    """Estimated revenue from appointments created in [start, end)."""
    prices = service_prices()
    q = session.query(Appointment).filter(
        Appointment.status.notin_(_REVENUE_EXCLUDED_STATUSES))
    if start:
        q = q.filter(Appointment.created_at >= start)
    if end:
        q = q.filter(Appointment.created_at < end)
    by_service: dict[str, dict] = {}
    total = 0.0
    for a in q.all():
        price = _price_for(a.service, prices)
        key = (a.service or "unknown").lower()
        e = by_service.setdefault(key, {"service": key, "count": 0, "revenue": 0.0})
        e["count"] += 1
        e["revenue"] += price
        total += price
    return {"estimated_total": round(total, 2),
            "by_service": sorted(by_service.values(),
                                 key=lambda x: -x["revenue"]),
            "note": "estimate: bookings x configured price (SERVICE_PRICES_JSON)"}


def get_analytics(session, days: int = 30) -> dict:
    """Call-quality analytics over the last N days."""
    tz = _tz()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    calls = (session.query(Call)
             .filter(Call.ended_at >= since)
             .order_by(Call.ended_at.desc()).all())

    hourly = [0] * 24                      # local-hour distribution
    weekday = [0] * 7                      # Mon..Sun
    weekday_booked = [0] * 7
    outcome_durations: dict[str, list[int]] = {}
    after_hours = short_calls = fallback_hits = 0
    unanswered_samples: list[str] = []

    for c in calls:
        ended = _aware(c.ended_at)
        if ended:
            local = ended.astimezone(tz)
            hourly[local.hour] += 1
            weekday[local.weekday()] += 1
            if c.booked:
                weekday_booked[local.weekday()] += 1
            if local.hour < 8 or local.hour >= 17 or local.weekday() >= 5:
                after_hours += 1
        if c.duration_seconds is not None and 0 < c.duration_seconds < 15:
            short_calls += 1
        if c.outcome and c.duration_seconds:
            outcome_durations.setdefault(c.outcome, []).append(c.duration_seconds)
        # unanswered-question mining (decrypt transcript, look for fallbacks)
        try:
            tx = (crypto.decrypt(c.transcript_enc) or "").lower()
        except Exception:
            tx = ""
        if tx and any(p in tx for p in _FALLBACK_PHRASES):
            fallback_hits += 1
            try:
                summary = crypto.decrypt(c.summary_enc)
            except Exception:
                summary = None
            if summary and len(unanswered_samples) < 5:
                unanswered_samples.append(summary[:160])

    total = len(calls)
    booked = sum(1 for c in calls if c.booked)
    abandoned = sum(1 for c in calls if c.outcome == "abandoned")
    return {
        "window_days": days,
        "total_calls": total,
        "booked": booked,
        "conversion_pct": round(booked / total * 100, 1) if total else 0,
        "abandoned": abandoned,
        "abandon_pct": round(abandoned / total * 100, 1) if total else 0,
        "short_calls_under_15s": short_calls,
        "after_hours_calls": after_hours,
        "hourly_distribution": hourly,
        "weekday_distribution": weekday,          # Mon..Sun
        "weekday_booked": weekday_booked,
        "avg_duration_by_outcome": {
            k: int(sum(v) / len(v)) for k, v in outcome_durations.items() if v},
        "faq_fallbacks": fallback_hits,
        "unanswered_samples": unanswered_samples,
    }


def no_show_stats(session) -> dict:
    """No-show rate for past appointments, split by reminder delivery."""
    now = dt.datetime.now(dt.timezone.utc)
    past = (session.query(Appointment)
            .filter(Appointment.start_utc < now).all())
    done = [a for a in past if a.status in ("completed", "confirmed", "showed",
                                            "noshow", "no_show")]
    shows = [a for a in done if a.status not in ("noshow", "no_show")]
    noshows = [a for a in done if a.status in ("noshow", "no_show")]

    def _rate(group):
        n = len(group)
        ns = sum(1 for a in group if a.status in ("noshow", "no_show"))
        return {"total": n, "no_shows": ns,
                "rate_pct": round(ns / n * 100, 1) if n else 0}

    with_rem = [a for a in done if a.reminder_sent]
    without_rem = [a for a in done if not a.reminder_sent]
    return {
        "past_appointments": len(done),
        "showed": len(shows),
        "no_shows": len(noshows),
        "no_show_rate_pct": round(len(noshows) / len(done) * 100, 1) if done else 0,
        "with_reminder": _rate(with_rem),
        "without_reminder": _rate(without_rem),
    }


def build_report(session, period: str = "week") -> dict:
    """Owner report for the last week or calendar month-to-date."""
    tz = _tz()
    now_local = dt.datetime.now(tz)
    if period == "month":
        start_local = now_local.replace(day=1, hour=0, minute=0,
                                        second=0, microsecond=0)
        label = now_local.strftime("%B %Y")
    else:
        period = "week"
        start_local = (now_local - dt.timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        label = f"week ending {now_local.date().isoformat()}"
    start = start_local.astimezone(dt.timezone.utc)

    calls, total = repository.list_recent_calls(session, limit=5000,
                                                date_from=start)
    booked = sum(1 for c in calls if c.booked)
    abandoned = sum(1 for c in calls if c.outcome == "abandoned")
    monthly = repository.monthly_new_patients(session, months=1)
    revenue = revenue_stats(session, start=start)
    noshow = no_show_stats(session)
    an = get_analytics(session, days=(31 if period == "month" else 7))

    lines = [
        f"{os.getenv('CLINIC_NAME', 'Bright Smile Dental')} — report, {label}",
        f"Calls: {total} | Booked: {booked} "
        f"({round(booked / total * 100) if total else 0}% conversion)",
        f"Abandoned: {abandoned} | After-hours calls: {an['after_hours_calls']}",
        f"New patients: {monthly[-1]['new_patients'] if monthly else 0} (this month)",
        f"Estimated revenue booked: ${revenue['estimated_total']:,.0f}",
    ]
    for s in revenue["by_service"][:5]:
        lines.append(f"  - {s['service']}: {s['count']} × → ${s['revenue']:,.0f}")
    lines.append(f"No-show rate: {noshow['no_show_rate_pct']}% "
                 f"(with reminder {noshow['with_reminder']['rate_pct']}%, "
                 f"without {noshow['without_reminder']['rate_pct']}%)")
    if an["faq_fallbacks"]:
        lines.append(f"Unanswered questions (add to FAQ?): {an['faq_fallbacks']}")

    return {"period": period, "label": label, "start": start.isoformat(),
            "text": "\n".join(lines),
            "calls": total, "booked": booked, "abandoned": abandoned,
            "revenue": revenue, "no_show": noshow,
            "new_patients_this_month": monthly[-1]["new_patients"] if monthly else 0,
            "analytics": an}
