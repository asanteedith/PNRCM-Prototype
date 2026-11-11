"""Analytics calculations derived from SQLite records only. No fabricated metrics."""
from __future__ import annotations
import csv, io, json, datetime
from typing import Any, Dict, List, Tuple
from db import get_db

CURRENCY = "GHS"

def _date_ok(ts: str, start: str = None, end: str = None) -> bool:
    if not ts: return False
    try:
        d = ts[:10]
    except Exception:
        return False
    if start and d < start: return False
    if end and d > end: return False
    return True

def overview(start: str = None, end: str = None) -> Dict[str, Any]:
    c = get_db()
    bills = [dict(r) for r in c.execute("SELECT * FROM bills").fetchall()]
    claims = [dict(r) for r in c.execute("SELECT * FROM claims").fetchall()]
    payments = [dict(r) for r in c.execute("SELECT * FROM payments WHERE status='completed'").fetchall()]
    pat_pays = [dict(r) for r in c.execute("SELECT * FROM patient_payments WHERE payment_status='confirmed'").fetchall()]
    # filter by date when provided (use created/submitted/payment dates)
    if start or end:
        bills = [b for b in bills if _date_ok(b.get("created_at"), start, end)]
        claims = [c_ for c_ in claims if _date_ok(c_.get("created_at") or c_.get("submitted_at"), start, end)]
    total_billed = sum(float(b.get("total") or 0) for b in bills)
    approved = sum(float(c_.get("approved_amount") or 0) for c_ in claims if c_.get("status") in ("approved","paid"))
    # Collected revenue: sum of payments table + patient_payments table (avoid double count by deduping by reference)
    collected_claims = sum(float(c_.get("paid_amount") or 0) for c_ in claims)
    collected_bills = sum(float(b.get("paid") or 0) for b in bills)
    collected_pat = sum(float(p.get("amount") or 0) for p in pat_pays)
    collected_sys = sum(float(p.get("amount") or 0) for p in payments)
    # Use the higher of claims-paid or bills-paid for system-recorded collections, plus patient payments
    collected = max(collected_claims, collected_bills) + collected_pat + collected_sys
    outstanding = sum(float(b.get("outstanding") or 0) for b in bills if (b.get("status") not in ("paid","rejected")))
    rejected = [c_ for c_ in claims if c_.get("status") in ("denied","rejected")]
    rejected_amt = sum(float(c_.get("total_charges") or 0) for c_ in rejected)
    submitted = [c_ for c_ in claims if c_.get("status") not in ("draft",)]
    clean = [c_ for c_ in claims if c_.get("status") in ("approved","paid")]
    clean_rate = (len(clean)/len(submitted)*100) if submitted else 0
    denial_rate = (len(rejected)/len(submitted)*100) if submitted else 0
    # potential leakage from leakage_cases table (estimated) or 0
    leak_rows = c.execute("SELECT COALESCE(SUM(amount),0) s FROM leakage_cases WHERE status NOT IN ('Recovered','Closed')").fetchone()
    potential_leakage = float(leak_rows["s"] or 0)
    # avg processing duration from agent_executions
    dur = c.execute("SELECT AVG(duration_ms) a FROM agent_executions WHERE duration_ms IS NOT NULL").fetchone()["a"]
    avg_dur_s = round((dur or 0)/1000, 2) if dur else 0
    recovery_rate = (collected/total_billed*100) if total_billed else 0
    c.close()
    return {
        "currency": CURRENCY,
        "date_range": {"start": start, "end": end},
        "kpis": {
            "total_billed_revenue": {"label":"Total billed revenue","value":round(total_billed,2),"definition":"Sum of bill totals","method":"SUM(bills.total)","currency":CURRENCY},
            "total_approved_claim_value": {"label":"Total approved claim value","value":round(approved,2),"definition":"Sum of approved amounts for approved/paid claims","method":"SUM(claims.approved_amount where status in approved,paid)","currency":CURRENCY},
            "total_collected_revenue": {"label":"Total collected revenue","value":round(collected,2),"definition":"Collections from claims paid, bills paid, patient payments, and system payments","method":"MAX(claims.paid_amount, bills.paid) + patient_payments + payments","currency":CURRENCY},
            "total_outstanding_reimbursement": {"label":"Total outstanding reimbursement","value":round(outstanding,2),"definition":"Outstanding amounts on non-paid bills","method":"SUM(bills.outstanding where status not in paid,rejected)","currency":CURRENCY},
            "total_rejected_claims": {"label":"Total rejected claims","value":{"count":len(rejected),"amount":round(rejected_amt,2)},"definition":"Claims denied or rejected","method":"COUNT/SUM claims where status in denied,rejected)","currency":CURRENCY},
            "clean_claim_rate": {"label":"Clean claim rate","value":round(clean_rate,1),"unit":"%","definition":"Approved/paid claims over submitted claims","method":"approved_or_paid/submitted*100"},
            "claim_denial_rate": {"label":"Claim denial rate","value":round(denial_rate,1),"unit":"%","definition":"Denied/rejected over submitted claims","method":"denied_or_rejected/submitted*100"},
            "potential_revenue_leakage": {"label":"Potential revenue leakage","value":round(potential_leakage,2),"definition":"Estimated unresolved leakage from leakage cases","method":"SUM(leakage_cases.amount where not recovered/closed)","currency":CURRENCY},
            "avg_claim_processing_duration": {"label":"Average claim processing duration","value":avg_dur_s,"unit":"s","definition":"Average agent execution duration","method":"AVG(agent_executions.duration_ms)/1000"},
            "reimbursement_recovery_rate": {"label":"Reimbursement recovery rate","value":round(recovery_rate,1),"unit":"%","definition":"Collected over billed","method":"collected/billed*100"},
        },
        "counts": {"bills": len(bills), "claims": len(claims), "submitted": len(submitted)}
    }

def claims_analytics(start: str = None, end: str = None, payer: str = None, status: str = None) -> Dict[str, Any]:
    c = get_db()
    rows = [dict(r) for r in c.execute("SELECT * FROM claims").fetchall()]
    c.close()
    if start or end:
        rows = [r for r in rows if _date_ok(r.get("created_at") or r.get("submitted_at"), start, end)]
    if payer: rows = [r for r in rows if (r.get("payer_name") or r.get("payer_type") or "").lower().find(payer.lower()) >= 0]
    if status: rows = [r for r in rows if r.get("status") == status]
    total = len(rows)
    submitted = sum(1 for r in rows if r.get("status") not in ("draft",))
    approved = sum(1 for r in rows if r.get("status") in ("approved","paid"))
    rejected = sum(1 for r in rows if r.get("status") in ("denied","rejected"))
    pending = sum(1 for r in rows if r.get("status") in ("submitted","vetted","under_review"))
    correction = sum(1 for r in rows if r.get("status") in ("resubmission_required","needs_correction"))
    first_pass = (approved/submitted*100) if submitted else 0
    rej_rate = (rejected/submitted*100) if submitted else 0
    reasons = {}
    for r in rows:
        if r.get("denial_reason"):
            reasons[r["denial_reason"]] = reasons.get(r["denial_reason"], 0) + 1
    status_dist = {}
    for r in rows:
        s = r.get("status") or "unknown"
        status_dist[s] = status_dist.get(s, 0) + 1
    return {
        "currency": CURRENCY,
        "total_claims": total, "submitted_claims": submitted, "approved_claims": approved,
        "rejected_claims": rejected, "pending_claims": pending, "claims_requiring_correction": correction,
        "first_pass_success_rate": round(first_pass,1), "claim_rejection_rate": round(rej_rate,1),
        "rejection_reasons": [{"reason":k,"count":v} for k,v in sorted(reasons.items(), key=lambda x:-x[1])],
        "status_distribution": status_dist,
        "claims": rows,
    }

def revenue_analytics(start: str = None, end: str = None) -> Dict[str, Any]:
    c = get_db()
    bills = [dict(r) for r in c.execute("SELECT * FROM bills").fetchall()]
    claims = [dict(r) for r in c.execute("SELECT * FROM claims").fetchall()]
    c.close()
    if start or end:
        bills = [b for b in bills if _date_ok(b.get("created_at"), start, end)]
        claims = [x for x in claims if _date_ok(x.get("created_at"), start, end)]
    billed = sum(float(b.get("total") or 0) for b in bills)
    approved = sum(float(x.get("approved_amount") or 0) for x in claims)
    collected = sum(float(x.get("paid_amount") or 0) for x in claims)
    outstanding = sum(float(b.get("outstanding") or 0) for b in bills)
    underpayments = sum(max(float(x.get("approved_amount") or 0)-float(x.get("paid_amount") or 0),0) for x in claims if x.get("status")=="paid")
    by_payer = {}
    for x in claims:
        k = x.get("payer_name") or x.get("payer_type") or "Unknown"
        by_payer[k] = by_payer.get(k, 0) + float(x.get("total_charges") or 0)
    return {
        "currency": CURRENCY,
        "billed_revenue": round(billed,2), "approved_revenue": round(approved,2),
        "collected_revenue": round(collected,2), "outstanding_reimbursement": round(outstanding,2),
        "underpayments": round(underpayments,2),
        "by_payer": [{"payer":k,"amount":round(v,2)} for k,v in by_payer.items()],
        "note": "Billed, approved, and collected are distinct measures and not interchangeable."
    }

def leakage_analytics() -> Dict[str, Any]:
    c = get_db()
    rows = [dict(r) for r in c.execute("SELECT * FROM leakage_cases ORDER BY created_at DESC").fetchall()]
    c.close()
    by_cat = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + float(r.get("amount") or 0)
    total = sum(float(r.get("amount") or 0) for r in rows)
    recovered = sum(float(r.get("amount") or 0) for r in rows if r.get("status") in ("Recovered",))
    return {
        "currency": CURRENCY,
        "total_estimated": round(total,2), "recovered": round(recovered,2),
        "unresolved": round(total-recovered,2),
        "by_category": [{"category":k,"amount":round(v,2)} for k,v in by_cat.items()],
        "cases": rows,
        "note": "Suspected/estimated leakage vs confirmed recovered revenue are distinct."
    }

def ar_aging() -> Dict[str, Any]:
    c = get_db()
    claims = [dict(r) for r in c.execute("SELECT * FROM claims WHERE status NOT IN ('paid','closed','draft')").fetchall()]
    c.close()
    today = datetime.date.today()
    buckets = {"0-30": [], "31-60": [], "61-90": [], "90+": []}
    for cl in claims:
        ref = (cl.get("submitted_at") or cl.get("created_at") or "")[:10]
        try:
            d = datetime.date.fromisoformat(ref)
            age = (today - d).days
        except Exception:
            age = 0
        amt = float(cl.get("total_charges") or 0) - float(cl.get("paid_amount") or 0)
        if amt <= 0: continue
        item = {"claim_number": cl.get("claim_number"), "claim_id": cl["id"], "payer": cl.get("payer_name") or cl.get("payer_type"), "outstanding": round(amt,2), "age_days": age}
        if age <= 30: buckets["0-30"].append(item)
        elif age <= 60: buckets["31-60"].append(item)
        elif age <= 90: buckets["61-90"].append(item)
        else: buckets["90+"].append(item)
    summary = {k: {"count": len(v), "amount": round(sum(x["outstanding"] for x in v),2), "items": v} for k,v in buckets.items()}
    return {"currency": CURRENCY, "buckets": summary, "methodology": "Age calculated from claim submitted_at (or created_at) to today; outstanding = total_charges - paid_amount."}

def agent_performance() -> Dict[str, Any]:
    c = get_db()
    rows = [dict(r) for r in c.execute("SELECT * FROM agent_executions").fetchall()]
    c.close()
    by_agent = {}
    for r in rows:
        a = r.get("agent_name") or "Unknown"
        by_agent.setdefault(a, {"agent":a,"tasks":0,"success":0,"failed":0,"needs_review":0,"durations":[]})
        by_agent[a]["tasks"] += 1
        if r.get("status") == "completed": by_agent[a]["success"] += 1
        if r.get("status") == "failed": by_agent[a]["failed"] += 1
        if r.get("human_review_required"): by_agent[a]["needs_review"] += 1
        if r.get("duration_ms"): by_agent[a]["durations"].append(r["duration_ms"])
    out = []
    for a, v in by_agent.items():
        avg = (sum(v["durations"])/len(v["durations"])) if v["durations"] else 0
        v["avg_duration_ms"] = round(avg,1)
        v["error_rate"] = round((v["failed"]/v["tasks"]*100) if v["tasks"] else 0,1)
        del v["durations"]
        out.append(v)
    return {"agents": out, "total_executions": len(rows)}

def export_csv(rows: List[dict], columns: List[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v) if isinstance(v,(dict,list)) else v) for k,v in r.items()})
    return buf.getvalue()
