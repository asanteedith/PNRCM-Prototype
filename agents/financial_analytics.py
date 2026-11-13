"""Financial Analytics & Trends Agent.

Read-only, data-derived analysis of PNRCM financial records. Produces revenue
trends, payer performance, claims/denial trends, AR aging, leakage trends,
simple forecasts, anomaly flags, and management insights.

Rules: never fabricate values; every number is derived from persisted records.
Forecasts are clearly labeled estimates. The agent does not modify records.
"""
from __future__ import annotations
import datetime, json
from typing import Any, Dict, List, Optional
from db import get_db
import agents.orchestrator as orch

CURRENCY = "GHS"


def _date_ok(ts, start=None, end=None):
    if not ts:
        return False
    d = str(ts)[:10]
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def _month(ts):
    return str(ts)[:7] if ts else None


def _f(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Revenue trend (billed vs collected by month)
# ---------------------------------------------------------------------------
def revenue_trends(start=None, end=None) -> Dict[str, Any]:
    c = get_db()
    bills = [dict(r) for r in c.execute("SELECT total, created_at FROM bills").fetchall()]
    pays = [dict(r) for r in c.execute("SELECT amount, created_at FROM payments WHERE status IN ('completed','confirmed')").fetchall()]
    pat = [dict(r) for r in c.execute("SELECT amount, payment_date FROM patient_payments WHERE payment_status='confirmed'").fetchall()]
    c.close()

    buckets: Dict[str, Dict[str, float]] = {}

    def _b(m):
        return buckets.setdefault(m, {"billed": 0.0, "collected": 0.0})

    for b in bills:
        if _date_ok(b["created_at"], start, end):
            _b(_month(b["created_at"]))["billed"] += _f(b["total"])
    for p in pays:
        if _date_ok(p["created_at"], start, end):
            _b(_month(p["created_at"]))["collected"] += _f(p["amount"])
    for p in pat:
        if _date_ok(p["payment_date"], start, end):
            _b(_month(p["payment_date"]))["collected"] += _f(p["amount"])

    months = sorted(buckets.keys())
    total_billed = round(sum(v["billed"] for v in buckets.values()), 2)
    total_collected = round(sum(v["collected"] for v in buckets.values()), 2)
    # growth rate last vs first month (billed)
    growth = None
    if len(months) >= 2:
        first = buckets[months[0]]["billed"]
        last = buckets[months[-1]]["billed"]
        if first > 0:
            growth = round((last - first) / first * 100, 1)

    return {
        "currency": CURRENCY,
        "months": [{"month": m, "billed": round(buckets[m]["billed"], 2),
                     "collected": round(buckets[m]["collected"], 2)} for m in months],
        "total_billed": total_billed,
        "total_collected": total_collected,
        "collection_rate_pct": round((total_collected / total_billed * 100), 1) if total_billed else 0.0,
        "billed_growth_pct": growth,
        "note": "Billed and collected are distinct; collected is grounded in confirmed payment records only.",
    }


# ---------------------------------------------------------------------------
# Payer performance
# ---------------------------------------------------------------------------
def payer_performance(start=None, end=None) -> Dict[str, Any]:
    c = get_db()
    claims = [dict(r) for r in c.execute("SELECT * FROM claims").fetchall()]
    pays = [dict(r) for r in c.execute("SELECT claim_id, amount FROM payments WHERE status IN ('completed','confirmed')").fetchall()]
    c.close()
    by: Dict[str, Dict[str, Any]] = {}
    for cl in claims:
        if not _date_ok(cl.get("created_at"), start, end):
            continue
        payer = cl.get("payer_name") or cl.get("payer_type") or "Unknown"
        p = by.setdefault(payer, {"payer": payer, "claims_submitted": 0, "claims_paid": 0,
                                   "claims_denied": 0, "billed": 0.0, "paid": 0.0, "outstanding": 0.0})
        p["claims_submitted"] += 1
        p["billed"] += _f(cl.get("total_charges"))
        p["paid"] += _f(cl.get("paid_amount"))
        p["outstanding"] += max(0.0, _f(cl.get("approved_amount")) - _f(cl.get("paid_amount")))
        if cl.get("status") in ("paid", "approved"):
            p["claims_paid"] += 1
        if cl.get("status") in ("denied", "rejected"):
            p["claims_denied"] += 1
    result = []
    for p in by.values():
        sub = p["claims_submitted"] or 1
        p["denial_rate_pct"] = round(p["claims_denied"] / sub * 100, 1)
        p["collection_rate_pct"] = round(p["paid"] / p["billed"] * 100, 1) if p["billed"] else 0.0
        for k in ("billed", "paid", "outstanding"):
            p[k] = round(p[k], 2)
        result.append(p)
    result.sort(key=lambda x: x["billed"], reverse=True)
    return {"currency": CURRENCY, "payers": result}


# ---------------------------------------------------------------------------
# Claims & denial trends
# ---------------------------------------------------------------------------
def claims_denial_trends(start=None, end=None) -> Dict[str, Any]:
    c = get_db()
    claims = [dict(r) for r in c.execute("SELECT * FROM claims").fetchall()]
    c.close()
    months: Dict[str, Dict[str, int]] = {}
    denial_reasons: Dict[str, int] = {}
    total = 0
    denied = 0
    for cl in claims:
        if not _date_ok(cl.get("created_at"), start, end):
            continue
        m = _month(cl.get("created_at"))
        b = months.setdefault(m, {"submitted": 0, "denied": 0})
        b["submitted"] += 1
        total += 1
        if cl.get("status") in ("denied", "rejected"):
            b["denied"] += 1
            denied += 1
            r = cl.get("denial_reason") or cl.get("denial_category") or "unspecified"
            denial_reasons[r] = denial_reasons.get(r, 0) + 1
    return {
        "currency": CURRENCY,
        "months": [{"month": m, "submitted": v["submitted"], "denied": v["denied"]} for m, v in sorted(months.items())],
        "total_claims": total,
        "denied_claims": denied,
        "denial_rate_pct": round(denied / total * 100, 1) if total else 0.0,
        "recurring_denial_reasons": sorted(
            [{"reason": k, "count": v} for k, v in denial_reasons.items()],
            key=lambda x: x["count"], reverse=True)[:10],
    }


# ---------------------------------------------------------------------------
# AR aging
# ---------------------------------------------------------------------------
def ar_aging(start=None, end=None) -> Dict[str, Any]:
    c = get_db()
    claims = [dict(r) for r in c.execute("SELECT * FROM claims WHERE status IN ('submitted','vetted','denied','paid','approved')").fetchall()]
    c.close()
    now = datetime.date.today()
    buckets = {"0-30": {"amount": 0.0, "count": 0}, "31-60": {"amount": 0.0, "count": 0},
               "61-90": {"amount": 0.0, "count": 0}, "90+": {"amount": 0.0, "count": 0}}
    for cl in claims:
        if not _date_ok(cl.get("created_at"), start, end):
            continue
        outstanding = max(0.0, _f(cl.get("approved_amount")) - _f(cl.get("paid_amount")))
        if outstanding <= 0.01:
            continue
        try:
            d = datetime.date.fromisoformat(str(cl.get("created_at"))[:10])
        except Exception:
            continue
        age = (now - d).days
        key = "0-30" if age <= 30 else "31-60" if age <= 60 else "61-90" if age <= 90 else "90+"
        buckets[key]["amount"] += outstanding
        buckets[key]["count"] += 1
    for k in buckets:
        buckets[k]["amount"] = round(buckets[k]["amount"], 2)
    total = round(sum(v["amount"] for v in buckets.values()), 2)
    return {"currency": CURRENCY, "buckets": buckets, "total_outstanding": total}


# ---------------------------------------------------------------------------
# Revenue leakage trends
# ---------------------------------------------------------------------------
def leakage_trends(start=None, end=None) -> Dict[str, Any]:
    c = get_db()
    cases = [dict(r) for r in c.execute("SELECT * FROM leakage_cases").fetchall()]
    c.close()
    def est(x):
        return _f(x["estimated_exposure"]) if x.get("estimated_exposure") is not None else _f(x.get("amount"))
    by_cat: Dict[str, Dict[str, Any]] = {}
    total_est = total_conf = total_rec = 0.0
    for x in cases:
        if not _date_ok(x.get("created_at"), start, end):
            continue
        cat = x.get("category") or "uncategorized"
        d = by_cat.setdefault(cat, {"category": cat, "count": 0, "estimated": 0.0, "confirmed": 0.0, "recovered": 0.0})
        d["count"] += 1
        d["estimated"] += est(x)
        d["confirmed"] += _f(x.get("confirmed_amount"))
        d["recovered"] += _f(x.get("verified_recovered_amount"))
        total_est += est(x); total_conf += _f(x.get("confirmed_amount")); total_rec += _f(x.get("verified_recovered_amount"))
    for d in by_cat.values():
        for k in ("estimated", "confirmed", "recovered"):
            d[k] = round(d[k], 2)
    return {
        "currency": CURRENCY,
        "by_category": sorted(by_cat.values(), key=lambda x: x["estimated"], reverse=True),
        "total_estimated_exposure": round(total_est, 2),
        "total_confirmed": round(total_conf, 2),
        "total_verified_recovered": round(total_rec, 2),
        "note": "Estimated exposure is potential, not confirmed loss; verified_recovered requires confirmed payment.",
    }


# ---------------------------------------------------------------------------
# Simple forecast (linear projection of collected revenue over months)
# ---------------------------------------------------------------------------
def forecast(months_ahead: int = 3) -> Dict[str, Any]:
    rev = revenue_trends()
    months = rev["months"]
    if len(months) < 3:
        return {"supported": False, "reason": "Insufficient historical months (need >=3) to produce a reliable forecast.",
                "currency": CURRENCY}
    ys = [m["collected"] for m in months]
    n = len(ys)
    # simple least-squares slope
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs) or 1
    slope = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)) / denom
    intercept = mean_y - slope * mean_x
    proj = []
    for h in range(1, months_ahead + 1):
        idx = n - 1 + h
        proj.append({"step": h, "estimated_collected": round(max(0.0, intercept + slope * idx), 2)})
    return {
        "supported": True,
        "currency": CURRENCY,
        "method": "linear least-squares on monthly collected revenue",
        "data_months": n,
        "assumptions": ["Assumes recent trend continues; excludes seasonality and one-off payments.",
                         "Forecast is an ESTIMATE, not a confirmed financial outcome."],
        "forecast": proj,
    }


# ---------------------------------------------------------------------------
# Anomaly detection (flag for review, not fraud)
# ---------------------------------------------------------------------------
def anomalies() -> Dict[str, Any]:
    rev = revenue_trends()
    months = rev["months"]
    flags = []
    if len(months) >= 2:
        # month-over-month collected swing > 50%
        for i in range(1, len(months)):
            prev = months[i - 1]["collected"]
            cur = months[i]["collected"]
            if prev > 0:
                chg = (cur - prev) / prev * 100
                if abs(chg) >= 50:
                    flags.append({"type": "revenue_swing", "month": months[i]["month"],
                                   "change_pct": round(chg, 1), "detail": f"Collected revenue changed {chg:.1f}% vs prior month."})
    # denial rate spike
    cd = claims_denial_trends()
    if cd["total_claims"] >= 5 and cd["denial_rate_pct"] >= 40:
        flags.append({"type": "denial_spike", "detail": f"Denial rate {cd['denial_rate_pct']}% is elevated (>=40%)."})
    # high outstanding AR
    ar = ar_aging()
    if ar["buckets"]["90+"]["amount"] > 0:
        flags.append({"type": "overdue_ar", "detail": f"GHS {ar['buckets']['90+']['amount']:.2f} outstanding >90 days."})
    return {"currency": CURRENCY, "flags": flags,
            "note": "Anomalies are flagged for human review, not classified as fraud or confirmed loss."}


# ---------------------------------------------------------------------------
# Management insights (combine the above into actionable statements)
# ---------------------------------------------------------------------------
def management_insights(start=None, end=None) -> Dict[str, Any]:
    rev = revenue_trends(start, end)
    payer = payer_performance(start, end)
    cd = claims_denial_trends(start, end)
    ar = ar_aging(start, end)
    lk = leakage_trends(start, end)
    ins = []
    if rev["billed_growth_pct"] is not None:
        direction = "increased" if rev["billed_growth_pct"] >= 0 else "declined"
        ins.append({"severity": "info", "title": f"Billed revenue {direction} {abs(rev['billed_growth_pct'])}%",
                     "detail": f"Across {len(rev['months'])} months, billed revenue {direction} between the first and latest month."})
    if rev["collection_rate_pct"] < 70 and rev["total_billed"] > 0:
        ins.append({"severity": "high", "title": f"Low collection rate ({rev['collection_rate_pct']}%)",
                     "detail": "Collected revenue is below 70% of billed. Review outstanding claims and payer follow-ups.",
                     "next_action": "Follow up with payers on outstanding claims.", "owner": "Finance Officer"})
    if cd["denial_rate_pct"] >= 20:
        top = cd["recurring_denial_reasons"][0]["reason"] if cd["recurring_denial_reasons"] else "unspecified"
        ins.append({"severity": "high", "title": f"Elevated denial rate ({cd['denial_rate_pct']}%)",
                     "detail": f"Top recurring denial reason: {top}.",
                     "next_action": "Review coding/documentation for the leading denial reason.", "owner": "Claims Officer"})
    if ar["buckets"]["90+"]["amount"] > 0:
        ins.append({"severity": "medium", "title": "Overdue receivables (>90 days)",
                     "detail": f"GHS {ar['buckets']['90+']['amount']:.2f} is overdue beyond 90 days across {ar['buckets']['90+']['count']} claim(s).",
                     "next_action": "Initiate follow-up or escalation.", "owner": "Revenue Cycle Manager"})
    if lk["total_estimated_exposure"] > 0:
        ins.append({"severity": "medium", "title": "Potential revenue leakage",
                     "detail": f"GHS {lk['total_estimated_exposure']:.2f} estimated exposure across {sum(c['count'] for c in lk['by_category'])} case(s).",
                     "next_action": "Review leakage cases; confirm and recover where valid.", "owner": "Revenue Cycle Manager"})
    if not ins:
        ins.append({"severity": "info", "title": "No significant issues detected",
                     "detail": "Current data does not indicate material revenue risks."})
    return {"currency": CURRENCY, "insights": ins}


# ---------------------------------------------------------------------------
# Full run (registers an orchestrator execution)
# ---------------------------------------------------------------------------
def run_full_analysis(start=None, end=None) -> Dict[str, Any]:
    c = get_db()
    eid = orch.agent_execution_start(c, "Financial Analytics & Trends Agent", "financial_analysis", "analytics", None,
                                     {"start": start, "end": end}, None)
    c.commit(); c.close()
    result = {
        "revenue_trends": revenue_trends(start, end),
        "payer_performance": payer_performance(start, end),
        "claims_denial_trends": claims_denial_trends(start, end),
        "ar_aging": ar_aging(start, end),
        "leakage_trends": leakage_trends(start, end),
        "forecast": forecast(),
        "anomalies": anomalies(),
        "management_insights": management_insights(start, end),
    }
    c = get_db()
    orch.agent_execution_complete(c, eid, "completed", {"period": [start, end]}, duration_ms=0)
    orch.set_agent_state("Financial Analytics & Trends Agent", "Completed", "financial_analysis",
                         {"generated": True})
    c.commit(); c.close()
    return result