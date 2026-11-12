"""Revenue Enhancements backend logic (Phases 1-6).

Extends existing PNRCM Revenue OS with:
- Command Center financial overview + Action Required (Phase 1)
- Revenue Integrity leakage analysis enhancements (Phase 2)
- Payer Intelligence analytics (Phase 3)
- Revenue Cycle Performance Scorecard (Phase 4)
- Human Review & Accountability enhancements (Phase 5)
- Revenue Impact Tracking lifecycle (Phase 6)

All calculations derived from persisted SQLite records only.
No hardcoded metrics, no fabricated data, no "Fraud" terminology.
"""
from __future__ import annotations

import json, uuid, datetime
from typing import Any, Dict, List, Optional, Tuple
from db import get_db, now, audit, agent_execution_start, agent_execution_complete, add_exception

CURRENCY = "GHS"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _money(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _round2(v: float) -> float:
    return round(_money(v), 2)


def _iso(d: Optional[str]) -> Optional[datetime.datetime]:
    if not d:
        return None
    try:
        return datetime.datetime.fromisoformat(d[:19])
    except Exception:
        return None


def _days_between(a: Optional[str], b: Optional[str]) -> Optional[int]:
    da, db = _iso(a), _iso(b)
    if da is None or db is None:
        return None
    return (db - da).days


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def _safe_get(row: dict, key: str, default=None):
    try:
        return row[key]
    except Exception:
        return default


# ===========================================================================
# Phase 1: Command Center - Enhanced Financial Overview + Action Required
# ===========================================================================

def command_center_overview() -> Dict[str, Any]:
    """Enhanced command center financial overview derived from persisted records.

    All metrics computed from actual database rows. No hardcoded values.
    Distinguishes estimated vs confirmed revenue.
    """
    c = get_db()
    bills = [dict(r) for r in c.execute("SELECT * FROM bills").fetchall()]
    claims = [dict(r) for r in c.execute("SELECT * FROM claims").fetchall()]
    encounters = [dict(r) for r in c.execute("SELECT * FROM encounters").fetchall()]
    patients = [dict(r) for r in c.execute("SELECT * FROM patients").fetchall()]
    payments_sys = [dict(r) for r in c.execute("SELECT * FROM payments WHERE status IN ('completed','confirmed')").fetchall()]
    pat_pays = [dict(r) for r in c.execute("SELECT * FROM patient_payments WHERE payment_status='confirmed'").fetchall()]
    reconciliations = [dict(r) for r in c.execute("SELECT * FROM reconciliations").fetchall()]
    exceptions_open = [dict(r) for r in c.execute("SELECT * FROM exceptions WHERE status='open'").fetchall()]
    review_tasks_pending = [dict(r) for r in c.execute("SELECT * FROM review_tasks WHERE status='pending'").fetchall()]
    denials_open = [dict(r) for r in c.execute("SELECT * FROM denials WHERE status NOT IN ('closed','resolved')").fetchall()]
    leakage_cases = [dict(r) for r in c.execute("SELECT * FROM leakage_cases").fetchall()]
    gap_cases = [dict(r) for r in c.execute("SELECT * FROM revenue_gap_cases").fetchall()]
    underpayment_cases = [dict(r) for r in c.execute("SELECT * FROM underpayment_cases").fetchall()]
    recovery_cases = [dict(r) for r in c.execute("SELECT * FROM recovery_cases").fetchall()]

    c.close()

    # ---- Financial metrics (from actual records) ----
    total_charges = _round2(sum(_money(b.get("total")) for b in bills))
    total_claims_submitted = sum(1 for cl in claims if cl.get("status") not in ("draft",))
    total_claims_approved = sum(1 for cl in claims if cl.get("status") in ("approved", "paid"))
    total_collected = _round2(
        sum(_money(b.get("paid")) for b in bills)
        + sum(_money(p.get("amount")) for p in payments_sys)
        + sum(_money(p.get("amount")) for p in pat_pays)
    )
    total_outstanding_ar = _round2(
        sum(_money(b.get("outstanding")) for b in bills if b.get("status") not in ("paid", "rejected"))
    )
    total_denied_value = _round2(
        sum(_money(cl.get("total_charges")) for cl in claims if cl.get("status") in ("denied", "rejected"))
    )
    total_denied_count = sum(1 for cl in claims if cl.get("status") in ("denied", "rejected"))

    # Clean claim rate
    submitted_claims = [cl for cl in claims if cl.get("status") not in ("draft",)]
    clean_claims = [cl for cl in claims if cl.get("status") in ("approved", "paid")]
    clean_claim_rate = round(len(clean_claims) / len(submitted_claims) * 100, 1) if submitted_claims else 0.0
    denial_rate = round(total_denied_count / len(submitted_claims) * 100, 1) if submitted_claims else 0.0

    # A/R aging from claims
    today = datetime.date.today()
    ar_buckets = {"0-30": {"count": 0, "amount": 0.0}, "31-60": {"count": 0, "amount": 0.0},
                  "61-90": {"count": 0, "amount": 0.0}, "90+": {"count": 0, "amount": 0.0}}
    for cl in claims:
        if cl.get("status") in ("paid", "closed", "draft"):
            continue
        ref = (cl.get("submitted_at") or cl.get("created_at") or "")[:10]
        try:
            d = datetime.date.fromisoformat(ref)
            age = (today - d).days
        except Exception:
            age = 0
        amt = _money(cl.get("total_charges")) - _money(cl.get("paid_amount"))
        if amt <= 0:
            continue
        bucket = "0-30" if age <= 30 else ("31-60" if age <= 60 else ("61-90" if age <= 90 else "90+"))
        ar_buckets[bucket]["count"] += 1
        ar_buckets[bucket]["amount"] = _round2(ar_buckets[bucket]["amount"] + amt)

    # Revenue at risk (open denials + unresolved leakage + outstanding underpayments)
    revenue_at_risk = _round2(
        total_denied_value
        + sum(_money(lc.get("amount")) for lc in leakage_cases if lc.get("status") not in ("Recovered", "Closed"))
        + sum(_money(uc.get("outstanding_amount")) for uc in underpayment_cases if uc.get("status") in ("open", "under_review"))
    )

    # Potential missed charges (from gap cases - estimated, not confirmed)
    potential_missed_charges = _round2(
        sum(_money(gc.get("amount_involved")) for gc in gap_cases if gc.get("status") == "Detected")
    )

    # Claims requiring immediate action (pending vetting, needs correction, resubmission)
    claims_requiring_action = sum(
        1 for cl in claims
        if cl.get("status") in ("submitted", "vetted", "needs_correction", "resubmission_required")
    )

    # Pending human reviews
    pending_human_reviews = len(review_tasks_pending)

    # Revenue recovered (confirmed only - from recovery_cases with verified amount)
    revenue_recovered = _round2(
        sum(_money(rc.get("verified_amount")) for rc in recovery_cases if rc.get("status") == "Recovered")
    )

    # Revenue protected (confirmed prevented loss - from gap cases moved to Under Review/Confirmed without double counting)
    # Protected = gap cases resolved without financial loss (charge captured, claim corrected)
    revenue_protected = _round2(
        sum(_money(gc.get("potential_recoverable")) for gc in gap_cases
          if gc.get("status") in ("Resolved", "Closed") and _money(gc.get("potential_recoverable")) > 0)
    )

    # Collection rate
    collection_rate = round(total_collected / total_charges * 100, 1) if total_charges else 0.0

    kpis = {
        "total_charges": {"label": "Total charges", "value": total_charges, "currency": CURRENCY,
                          "definition": "Sum of all bill totals", "method": "SUM(bills.total)"},
        "total_claims_submitted": {"label": "Total claims submitted", "value": total_claims_submitted,
                                   "definition": "Claims with status other than draft", "method": "COUNT(claims where status != draft)"},
        "total_claims_approved": {"label": "Total approved claims", "value": total_claims_approved,
                                  "definition": "Claims with status approved or paid", "method": "COUNT(claims where status in approved,paid)"},
        "total_collections": {"label": "Total collections", "value": total_collected, "currency": CURRENCY,
                              "definition": "Sum of bill payments + system payments + confirmed patient payments",
                              "method": "SUM(bills.paid) + SUM(payments.amount where completed) + SUM(patient_payments.amount where confirmed)"},
        "outstanding_accounts_receivable": {"label": "Outstanding A/R", "value": total_outstanding_ar, "currency": CURRENCY,
                                            "definition": "Outstanding amounts on non-paid bills",
                                            "method": "SUM(bills.outstanding where status not in paid,rejected)"},
        "ar_aging": {"label": "A/R aging", "value": ar_buckets, "currency": CURRENCY,
                     "definition": "Outstanding claim amounts aged from submission date",
                     "method": "Bucket by (today - submitted_at) days"},
        "denied_claims": {"label": "Denied claims", "value": total_denied_count, "currency": CURRENCY,
                          "definition": "Count and value of denied/rejected claims",
                          "method": "COUNT/SUM claims where status in denied,rejected"},
        "denied_value": {"label": "Denied value", "value": total_denied_value, "currency": CURRENCY,
                         "definition": "Sum of total_charges on denied/rejected claims"},
        "clean_claim_rate": {"label": "Clean claim rate", "value": clean_claim_rate, "unit": "%",
                             "definition": "Approved/paid claims over submitted claims",
                             "method": "approved_or_paid/submitted*100"},
        "revenue_at_risk": {"label": "Revenue at risk", "value": revenue_at_risk, "currency": CURRENCY,
                            "definition": "Denied value + unresolved leakage + outstanding underpayments (estimated, not confirmed)"},
        "potential_missed_charges": {"label": "Potential missed charges", "value": potential_missed_charges, "currency": CURRENCY,
                                     "definition": "Estimated unbilled/missing charges from open gap cases (estimated, not confirmed)"},
        "claims_requiring_immediate_action": {"label": "Claims requiring immediate action", "value": claims_requiring_action,
                                              "definition": "Claims in submitted/vetted/needs_correction/resubmission_required status"},
        "pending_human_reviews": {"label": "Pending human reviews", "value": pending_human_reviews,
                                  "definition": "Count of review tasks with status pending"},
        "revenue_recovered": {"label": "Revenue recovered", "value": revenue_recovered, "currency": CURRENCY,
                              "definition": "Confirmed recovered revenue from verified recovery cases only",
                              "method": "SUM(recovery_cases.verified_amount where status=Recovered)"},
        "revenue_protected": {"label": "Revenue protected", "value": revenue_protected, "currency": CURRENCY,
                              "definition": "Confirmed protected revenue from resolved gap cases (not double-counted as recovered)"},
        "collection_rate": {"label": "Collection rate", "value": collection_rate, "unit": "%",
                            "definition": "Collected amount relative to total charges",
                            "method": "collected/total_charges*100"},
    }

    return {
        "currency": CURRENCY,
        "kpis": kpis,
        "counts": {
            "bills": len(bills),
            "claims": len(claims),
            "submitted": len(submitted_claims),
            "encounters": len(encounters),
            "patients": len(patients),
            "open_exceptions": len(exceptions_open),
            "pending_reviews": pending_human_reviews,
            "open_denials": total_denied_count,
            "unresolved_leakage": len([l for l in leakage_cases if l.get("status") not in ("Recovered", "Closed")]),
        },
        "distinctions": {
            "estimated_vs_confirmed": "Estimated revenue (potential missed charges, revenue at risk) is not confirmed recovered revenue",
            "recovered_vs_protected": "Revenue recovered requires verified payment; revenue protected is prevented loss, not received cash",
        },
    }


def command_center_actions(limit: int = 20) -> Dict[str, Any]:
    """Prioritized Action Required section derived from persisted records.

    Each action includes: issue description, reference, responsible role, priority,
    due date, financial impact, recommended action, current status.
    """
    c = get_db()
    actions = []

    # --- Open denials as actions ---
    denials = c.execute(
        "SELECT * FROM denials WHERE status NOT IN ('closed','resolved') ORDER BY created_at DESC"
    ).fetchall()
    for d in denials:
        dd = dict(d)
        claim = c.execute("SELECT * FROM claims WHERE id=?", (dd["claim_id"],)).fetchone() if dd.get("claim_id") else None
        claim_num = dict(claim).get("claim_number") if claim else dd.get("claim_id")
        amount = _money(dict(claim).get("total_charges")) if claim else 0.0
        actions.append({
            "action_id": f"ACT-{dd['id'][:8]}",
            "issue_description": f"Denied claim {claim_num} requires corrective action: {dd.get('denial_reason') or 'denied'}",
            "reference_type": "claim",
            "reference_id": claim_num or dd["id"],
            "responsible_role": "Claims Officer",
            "priority": "high",
            "due_date": None,
            "financial_impact": _round2(amount),
            "recommended_action": dd.get("corrective_action") or "Review denial reason and determine corrective action",
            "current_status": dd.get("status", "open"),
            "source": "denial",
        })

    # --- Unresolved leakage cases as actions ---
    leaks = c.execute(
        "SELECT * FROM leakage_cases WHERE status NOT IN ('Recovered','Closed') ORDER BY amount DESC LIMIT 10"
    ).fetchall()
    for lk in leaks:
        ld = dict(lk)
        entity_ref = ld.get("entity_id") or ld.get("claim_id") or ld["id"]
        actions.append({
            "action_id": f"ACT-LK-{ld['id'][:8]}",
            "issue_description": f"Revenue leakage: {ld.get('category')} involving {entity_ref}",
            "reference_type": ld.get("entity_type", "case"),
            "reference_id": entity_ref,
            "responsible_role": "Billing Officer",
            "priority": "high" if _money(ld.get("amount")) >= 500 else "medium",
            "due_date": None,
            "financial_impact": _round2(ld.get("amount")),
            "recommended_action": ld.get("recommended_action") or "Investigate leakage finding",
            "current_status": ld.get("status", "Detected"),
            "source": "leakage",
        })

    # --- Open underpayment cases as actions ---
    underpayments = c.execute(
        "SELECT * FROM underpayment_cases WHERE status IN ('open','under_review') ORDER BY outstanding_amount DESC LIMIT 10"
    ).fetchall()
    for uc in underpayments:
        ud = dict(uc)
        claim_num = ud.get("claim_number") or ud.get("claim_id")
        actions.append({
            "action_id": f"ACT-UP-{ud['id'][:8]}",
            "issue_description": f"Underpayment on claim {claim_num}: outstanding {_money(ud.get('outstanding_amount')):.2f} {CURRENCY}",
            "reference_type": "claim",
            "reference_id": claim_num or ud["id"],
            "responsible_role": "Reconciliation Agent",
            "priority": "high" if _money(ud.get("outstanding_amount")) >= 500 else "medium",
            "due_date": None,
            "financial_impact": _round2(ud.get("outstanding_amount")),
            "recommended_action": ud.get("recommended_action") or "Pursue underpayment with payer or adjust balances",
            "current_status": ud.get("status", "open"),
            "source": "underpayment",
        })

    # --- Open exceptions as actions ---
    exceptions = c.execute(
        "SELECT * FROM exceptions WHERE status='open' ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    for ex in exceptions:
        ed = dict(ex)
        ref = ed.get("claim_id") or ed.get("encounter_id") or ed.get("patient_id") or ed["id"]
        actions.append({
            "action_id": f"ACT-EX-{ed['id'][:8]}",
            "issue_description": ed.get("title") or "Open exception requiring review",
            "reference_type": "exception",
            "reference_id": ref,
            "responsible_role": ed.get("detecting_agent") or "Revenue Integrity Agent",
            "priority": ed.get("severity", "medium"),
            "due_date": None,
            "financial_impact": 0.0,
            "recommended_action": ed.get("recommended_action") or "Review and resolve exception",
            "current_status": ed.get("status", "open"),
            "source": "exception",
        })

    # --- Pending review tasks as actions ---
    review_tasks = c.execute(
        "SELECT * FROM review_tasks WHERE status='pending' ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    for rt in review_tasks:
        rtd = dict(rt)
        actions.append({
            "action_id": f"ACT-RT-{rtd['id'][:8]}",
            "issue_description": rtd.get("context") or f"Review task {rtd['task_type']} for {rtd['entity_type']} {rtd['entity_id']}",
            "reference_type": rtd.get("entity_type", "task"),
            "reference_id": rtd.get("entity_id") or rtd["id"],
            "responsible_role": rtd.get("assigned_to") or "Human Reviewer",
            "priority": rtd.get("priority", "medium"),
            "due_date": rtd.get("due_date"),
            "financial_impact": _round2(rtd.get("financial_impact")),
            "recommended_action": rtd.get("ai_recommendation") or "Review and make decision",
            "current_status": rtd.get("status", "pending"),
            "source": "review_task",
        })

    # Sort by priority (high first) then financial impact (desc)
    priority_order = {"high": 0, "medium": 1, "low": 2}
    actions.sort(key=lambda a: (priority_order.get(a["priority"], 9), -a["financial_impact"]))

    c.close()
    return {
        "actions": actions[:limit],
        "count": len(actions),
        "returned": min(limit, len(actions)),
        "currency": CURRENCY,
    }


# ===========================================================================
# Phase 2: Revenue Integrity - Leakage Analysis Enhancements
# ===========================================================================

def leakage_findings(filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Enhanced revenue leakage findings with full audit details.

    Each finding: finding ID, category, record references, evidence, severity,
    confidence, estimated financial impact, recommended action, assigned owner,
    review status, resolution history.
    """
    c = get_db()
    f = filters or {}
    q = """SELECT lc.*, c.claim_number, c.payer_name, e.encounter_id
           FROM leakage_cases lc
           LEFT JOIN claims c ON c.id = lc.claim_id
           LEFT JOIN encounters e ON e.id = lc.encounter_id
           WHERE 1=1"""
    params: List[Any] = []
    if f.get("status"):
        q += " AND lc.status=?"; params.append(f["status"])
    if f.get("category"):
        q += " AND lc.category=?"; params.append(f["category"])
    if f.get("severity"):
        q += " AND lc.severity=?"; params.append(f["severity"])
    q += " ORDER BY lc.created_at DESC"

    rows = c.execute(q, params).fetchall()
    findings = []
    for r in rows:
        rd = dict(r)
        # Confidence derived from data completeness (not fabricated)
        confidence = _compute_leakage_confidence(rd)
        severity = _compute_leakage_severity(rd)
        findings.append({
            "finding_id": rd["id"],
            "category": rd.get("category"),
            "subcategory": rd.get("category"),
            "related_claim_id": rd.get("claim_id"),
            "related_claim_number": rd.get("claim_number"),
            "related_encounter_id": rd.get("encounter_id"),
            "related_entity_type": rd.get("entity_type"),
            "related_entity_id": rd.get("entity_id"),
            "evidence": rd.get("evidence"),
            "evidence_summary": _summarize_evidence(rd.get("evidence")),
            "severity": severity,
            "confidence": confidence,
            "estimated_financial_impact": _round2(rd.get("amount")),
            "confirmed_recoverable": 0.0,  # confirmed only after verification
            "recommended_action": rd.get("recommended_action"),
            "assigned_owner": "Billing Officer",
            "review_status": rd.get("status", "Detected"),
            "resolution_history": [],  # populated from audit_trail
            "created_at": rd.get("created_at"),
            "updated_at": rd.get("updated_at"),
            "currency": CURRENCY,
        })

    # Attach resolution history from audit_trail
    for finding in findings:
        audits = c.execute(
            """SELECT at.timestamp, at.actor, at.action, at.previous_state, at.new_state, at.decision
               FROM audit_trail at
               WHERE at.entity_id=? AND at.agent_name LIKE '%Revenue%'
               ORDER BY at.timestamp""",
            (finding["finding_id"],),
        ).fetchall()
        finding["resolution_history"] = [
            {
                "timestamp": a["timestamp"],
                "actor": a["actor"],
                "action": a["action"],
                "previous_state": a["previous_state"],
                "new_state": a["new_state"],
                "decision": a["decision"],
            } for a in audits
        ]

    total_estimated = _round2(sum(f["estimated_financial_impact"] for f in findings))
    total_confirmed = _round2(sum(f["confirmed_recoverable"] for f in findings))

    c.close()
    return {
        "currency": CURRENCY,
        "findings": findings,
        "count": len(findings),
        "total_estimated_impact": total_estimated,
        "total_confirmed_recoverable": total_confirmed,
        "distinctions": {
            "estimated_vs_confirmed": "Estimated financial impact is not confirmed recoverable revenue until verified",
            "potential_not_fraud": "Findings are potential leakage indicators, not confirmed fraud or misconduct",
        },
    }


def _compute_leakage_confidence(finding: dict) -> str:
    """Derive confidence from data completeness (not fabricated)."""
    score = 0
    if finding.get("evidence"): score += 1
    if finding.get("claim_id"): score += 1
    if finding.get("encounter_id"): score += 1
    if finding.get("entity_id"): score += 1
    if _money(finding.get("amount")) > 0: score += 1
    if score >= 4: return "high"
    if score >= 2: return "medium"
    return "low"


def _compute_leakage_severity(finding: dict) -> str:
    """Derive severity from amount and category (not fabricated)."""
    amount = _money(finding.get("amount"))
    category = (finding.get("category") or "").lower()
    if amount >= 1000 or "duplicate" in category:
        return "high"
    if amount >= 200:
        return "medium"
    return "low"


def _summarize_evidence(evidence_json: Optional[str]) -> str:
    if not evidence_json:
        return "No structured evidence available"
    try:
        ev = json.loads(evidence_json)
        if isinstance(ev, dict):
            parts = []
            for k, v in ev.items():
                if v is not None and v != "":
                    parts.append(f"{k}={v}")
            return "; ".join(parts[:6]) if parts else "No structured evidence available"
        return str(evidence_json)[:200]
    except Exception:
        return str(evidence_json)[:200]


# ===========================================================================
# Phase 3: Payer Intelligence
# ===========================================================================

def payer_intelligence(filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Payer performance analysis from actual persisted records.

    Supports NHIS, private insurance, self-pay scenarios.
    All metrics computed from claims, payments, denials, follow-ups, authorizations.
    """
    c = get_db()
    f = filters or {}

    claims = [dict(r) for r in c.execute("SELECT * FROM claims").fetchall()]
    payments = [dict(r) for r in c.execute("SELECT * FROM payments WHERE status IN ('completed','confirmed')").fetchall()]
    denials = [dict(r) for r in c.execute("SELECT * FROM denials").fetchall()]
    followups = [dict(r) for r in c.execute("SELECT * FROM claim_followups").fetchall()]
    authorizations = [dict(r) for r in c.execute("SELECT * FROM authorizations").fetchall()]

    # Filter by payer if specified
    payer_filter = f.get("payer")
    if payer_filter:
        claims = [cl for cl in claims if (cl.get("payer_name") or cl.get("payer_type") or "").lower() == payer_filter.lower()]

    # Group by payer
    payers: Dict[str, Dict[str, Any]] = {}
    for cl in claims:
        payer_name = cl.get("payer_name") or cl.get("payer_type") or "Unknown"
        if payer_filter and payer_name.lower() != payer_filter.lower():
            continue
        p = payers.setdefault(payer_name, {
            "payer": payer_name,
            "claims_submitted": 0,
            "claims_accepted": 0,
            "claims_denied": 0,
            "total_charges": 0.0,
            "total_approved": 0.0,
            "total_collected": 0.0,
            "turnaround_times": [],
            "denial_reasons": {},
            "authorization_issues": 0,
            "followup_count": 0,
        })
        p["claims_submitted"] += 1
        p["total_charges"] += _money(cl.get("total_charges"))
        p["total_approved"] += _money(cl.get("approved_amount"))
        status = cl.get("status", "unknown")
        if status in ("approved", "paid"):
            p["claims_accepted"] += 1
        elif status in ("denied", "rejected"):
            p["claims_denied"] += 1
            dr = cl.get("denial_reason") or "unspecified"
            p["denial_reasons"][dr] = p["denial_reasons"].get(dr, 0) + 1

        # Turnaround time: created_at to paid_at or submitted_at
        created = cl.get("created_at")
        paid = cl.get("paid_at") or cl.get("submitted_at")
        if created and paid:
            days = _days_between(created, paid)
            if days is not None:
                p["turnaround_times"].append(days)

        # Authorization issues
        auth = c.execute("SELECT * FROM authorizations WHERE claim_id=? OR encounter_id IN (SELECT id FROM encounters WHERE patient_id=(SELECT patient_id FROM claims WHERE id=?))",
                         (cl["id"], cl["id"])).fetchall()
        if not auth and status in ("denied", "rejected"):
            dr_lower = (cl.get("denial_reason") or "").lower()
            if "auth" in dr_lower or "authorization" in dr_lower:
                p["authorization_issues"] += 1

    # Add payments collected per payer
    for pay in payments:
        claim = c.execute("SELECT * FROM claims WHERE id=?", (pay.get("claim_id"),)).fetchone()
        if claim:
            cd = dict(claim)
            payer_name = cd.get("payer_name") or cd.get("payer_type") or "Unknown"
            if payer_filter and payer_name.lower() != payer_filter.lower():
                continue
            if payer_name in payers:
                payers[payer_name]["total_collected"] = _round2(payers[payer_name]["total_collected"] + _money(pay.get("amount")))

    # Add follow-up counts per payer
    for fu in followups:
        claim = c.execute("SELECT * FROM claims WHERE id=?", (fu.get("claim_id"),)).fetchone()
        if claim:
            cd = dict(claim)
            payer_name = cd.get("payer_name") or cd.get("payer_type") or "Unknown"
            if payer_filter and payer_name.lower() != payer_filter.lower():
                continue
            if payer_name in payers:
                payers[payer_name]["followup_count"] += 1

    # Compute derived metrics
    result = []
    for payer_name, p in payers.items():
        submitted = p["claims_submitted"]
        accepted = p["claims_accepted"]
        denied = p["claims_denied"]
        avg_tat = round(sum(p["turnaround_times"]) / len(p["turnaround_times"]), 1) if p["turnaround_times"] else 0.0
        denial_rate = round(denied / submitted * 100, 1) if submitted else 0.0
        acceptance_rate = round(accepted / submitted * 100, 1) if submitted else 0.0
        avg_claim_value = round(p["total_charges"] / submitted, 2) if submitted else 0.0

        # Outstanding A/R for this payer
        outstanding_ar = _round2(sum(_money(cl.get("total_charges")) - _money(cl.get("paid_amount"))
                                     for cl in claims
                                     if (cl.get("payer_name") or cl.get("payer_type") or "Unknown") == payer_name
                                     and cl.get("status") not in ("paid", "closed", "draft")))

        # Underpayment frequency: paid claims where paid < approved
        underpayment_freq = sum(
            1 for cl in claims
            if (cl.get("payer_name") or cl.get("payer_type") or "Unknown") == payer_name
            and cl.get("status") == "paid"
            and _money(cl.get("paid_amount")) < _money(cl.get("approved_amount"))
        )

        common_denial_categories = sorted(p["denial_reasons"].items(), key=lambda x: -x[1])[:5]

        result.append({
            "payer": payer_name,
            "claims_submitted": submitted,
            "claims_accepted": accepted,
            "claims_denied": denied,
            "denial_rate": denial_rate,
            "acceptance_rate": acceptance_rate,
            "average_reimbursement_turnaround_days": avg_tat,
            "outstanding_accounts_receivable": outstanding_ar,
            "underpayment_frequency": underpayment_freq,
            "common_denial_categories": [{"reason": k, "count": v} for k, v in common_denial_categories],
            "authorization_related_issues": p["authorization_issues"],
            "followup_workload": p["followup_count"],
            "total_collected": _round2(p["total_collected"]),
            "total_charges": _round2(p["total_charges"]),
            "average_claim_value": avg_claim_value,
            "currency": CURRENCY,
        })

    result.sort(key=lambda x: -x["total_collected"])

    c.close()
    return {
        "currency": CURRENCY,
        "payers": result,
        "count": len(result),
        "note": "All metrics computed from persisted claims, payments, denials, follow-ups, and authorizations. No fabricated data.",
    }


# ===========================================================================
# Phase 4: Revenue Cycle Performance Scorecard
# ===========================================================================

def performance_scorecard(start: Optional[str] = None, end: Optional[str] = None,
                          facility_id: Optional[str] = None,
                          department: Optional[str] = None) -> Dict[str, Any]:
    """Revenue Cycle Performance Scorecard with facility/department analysis.

    KPIs: Clean Claim Rate, Denial Rate, A/R Days, Collection Rate,
          Charge Capture Rate, Claim Turnaround Time, Denial Resolution Time,
          Revenue Recovery.
    """
    c = get_db()

    # Build WHERE clause for date/facility/department filters
    where_clauses = []
    params: List[Any] = []
    if start:
        where_clauses.append("created_at >= ?"); params.append(start)
    if end:
        where_clauses.append("created_at <= ?"); params.append(end)
    if facility_id:
        where_clauses.append("facility_id=?"); params.append(facility_id)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Claims
    claims = [dict(r) for r in c.execute(f"SELECT * FROM claims{where_sql}", params).fetchall()]
    # Bills
    bills = [dict(r) for r in c.execute(f"SELECT * FROM bills{where_sql}", params).fetchall()]
    # Payments
    payments_sys = [dict(r) for r in c.execute(
        f"SELECT * FROM payments WHERE status IN ('completed','confirmed'){where_sql}", params
    ).fetchall()]
    pat_pays = [dict(r) for r in c.execute(
        f"SELECT * FROM patient_payments WHERE payment_status='confirmed'{where_sql}", params
    ).fetchall()]
    # Denials
    denials = [dict(r) for r in c.execute(f"SELECT * FROM denials{where_sql}", params).fetchall()]
    # Follow-ups
    followups = [dict(r) for r in c.execute(f"SELECT * FROM claim_followups{where_sql}", params).fetchall()]
    # Recovery cases
    recovery_cases = [dict(r) for r in c.execute(f"SELECT * FROM recovery_cases{where_sql}", params).fetchall()]

    c.close()

    submitted = [cl for cl in claims if cl.get("status") not in ("draft",)]
    approved = [cl for cl in claims if cl.get("status") in ("approved", "paid")]
    rejected = [cl for cl in claims if cl.get("status") in ("denied", "rejected")]

    # ---- Clean Claim Rate ----
    clean_claim_rate = round(len(approved) / len(submitted) * 100, 1) if submitted else 0.0
    clean_claim_data = {
        "value": clean_claim_rate,
        "unit": "%",
        "definition": "Percentage of submitted claims accepted without preventable errors",
        "calculation": "approved_or_paid_claims / submitted_claims * 100",
        "numerator": len(approved),
        "denominator": len(submitted),
        "source": "claims table (status field)",
        "period": {"start": start, "end": end},
        "missing_data": len(submitted) == 0,
    }

    # ---- Denial Rate ----
    denial_rate = round(len(rejected) / len(submitted) * 100, 1) if submitted else 0.0
    denial_data = {
        "value": denial_rate,
        "unit": "%",
        "definition": "Percentage of submitted claims denied",
        "calculation": "denied_or_rejected_claims / submitted_claims * 100",
        "numerator": len(rejected),
        "denominator": len(submitted),
        "source": "claims table (status field)",
        "period": {"start": start, "end": end},
        "missing_data": len(submitted) == 0,
    }

    # ---- A/R Days ----
    # Average age of outstanding claims (submitted but not paid/closed)
    outstanding_claims = [cl for cl in claims if cl.get("status") not in ("paid", "closed", "draft")]
    today = datetime.date.today()
    ar_ages = []
    for cl in outstanding_claims:
        ref = (cl.get("submitted_at") or cl.get("created_at") or "")[:10]
        try:
            d = datetime.date.fromisoformat(ref)
            ar_ages.append((today - d).days)
        except Exception:
            pass
    ar_days = round(sum(ar_ages) / len(ar_ages), 1) if ar_ages else 0.0
    ar_days_data = {
        "value": ar_days,
        "unit": "days",
        "definition": "Average time revenue remains outstanding",
        "calculation": "AVG(today - submitted_at) for outstanding claims",
        "numerator": sum(ar_ages),
        "denominator": len(ar_ages),
        "source": "claims table (submitted_at, created_at, status)",
        "period": {"start": start, "end": end},
        "missing_data": len(ar_ages) == 0,
    }

    # ---- Collection Rate ----
    total_billed = _round2(sum(_money(b.get("total")) for b in bills))
    total_collected = _round2(
        sum(_money(p.get("amount")) for p in payments_sys)
        + sum(_money(p.get("amount")) for p in pat_pays)
    )
    collection_rate = round(total_collected / total_billed * 100, 1) if total_billed else 0.0
    collection_data = {
        "value": collection_rate,
        "unit": "%",
        "definition": "Collected amount relative to expected collectible revenue",
        "calculation": "total_collected / total_billed * 100",
        "numerator": total_collected,
        "denominator": total_billed,
        "source": "bills.total, payments.amount, patient_payments.amount",
        "period": {"start": start, "end": end},
        "missing_data": total_billed == 0,
    }

    # ---- Charge Capture Rate ----
    # Percentage of expected billable charges successfully captured
    # Valid denominator exists when services > 0
    unbilled = [s for s in c.execute("SELECT * FROM services WHERE billed=0").fetchall()] if False else []
    # Re-query services without the where_sql since services table doesn't have created_at filter in same way
    c2 = get_db()
    services = [dict(r) for r in c2.execute("SELECT * FROM services").fetchall()]
    c2.close()
    billed_services = [s for s in services if _money(s.get("billed")) > 0 or s.get("billed") == 1]
    unbilled_services = [s for s in services if not (_money(s.get("billed")) > 0 or s.get("billed") == 1)]
    total_services = len(services)
    charge_capture_rate = round(len(billed_services) / total_services * 100, 1) if total_services else 0.0
    charge_capture_data = {
        "value": charge_capture_rate,
        "unit": "%",
        "definition": "Percentage of expected billable charges successfully captured",
        "calculation": "billed_services / total_services * 100 (only when valid denominator exists)",
        "numerator": len(billed_services),
        "denominator": total_services,
        "source": "services table (billed field)",
        "period": {"start": start, "end": end},
        "missing_data": total_services == 0,
        "note": "Only reported when services exist; no denominator = no metric.",
    }

    # ---- Claim Turnaround Time ----
    # Time from encounter completion to claim submission
    turnaround_times = []
    for cl in claims:
        enc = c.execute("SELECT * FROM encounters WHERE id=?", (cl.get("encounter_id"),)).fetchone() if cl.get("encounter_id") else None
        if enc:
            ed = dict(enc)
            comp = ed.get("discharge_date") or ed.get("updated_at")
            sub = cl.get("submitted_at") or cl.get("created_at")
            if comp and sub:
                days = _days_between(comp, sub)
                if days is not None and days >= 0:
                    turnaround_times.append(days)
    c.close()
    avg_turnaround = round(sum(turnaround_times) / len(turnaround_times), 1) if turnaround_times else 0.0
    turnaround_data = {
        "value": avg_turnaround,
        "unit": "days",
        "definition": "Time from encounter completion to claim submission",
        "calculation": "AVG(claim.submitted_at - encounter.completion_date)",
        "numerator": sum(turnaround_times),
        "denominator": len(turnaround_times),
        "source": "encounters table (discharge_date/updated_at), claims table (submitted_at)",
        "period": {"start": start, "end": end},
        "missing_data": len(turnaround_times) == 0,
    }

    # ---- Denial Resolution Time ----
    # Time from denial to resolution for resolved denials
    resolution_times = []
    for d in denials:
        if d.get("status") in ("closed", "resolved") and d.get("resolved_at"):
            created = d.get("created_at")
            resolved = d.get("resolved_at")
            days = _days_between(created, resolved)
            if days is not None:
                resolution_times.append(days)
    avg_resolution = round(sum(resolution_times) / len(resolution_times), 1) if resolution_times else 0.0
    resolution_data = {
        "value": avg_resolution,
        "unit": "days",
        "definition": "Time required to resolve denied claims",
        "calculation": "AVG(denial.resolved_at - denial.created_at) for resolved denials",
        "numerator": sum(resolution_times),
        "denominator": len(resolution_times),
        "source": "denials table (created_at, resolved_at, status)",
        "period": {"start": start, "end": end},
        "missing_data": len(resolution_times) == 0,
    }

    # ---- Revenue Recovery ----
    # Confirmed additional revenue collected after resolving identified issues
    revenue_recovery = _round2(sum(_money(rc.get("verified_amount")) for rc in recovery_cases if rc.get("status") == "Recovered"))
    recovery_data = {
        "value": revenue_recovery,
        "currency": CURRENCY,
        "definition": "Confirmed additional revenue collected after resolving previously identified issues",
        "calculation": "SUM(recovery_cases.verified_amount where status=Recovered)",
        "source": "recovery_cases table (verified_amount, status)",
        "period": {"start": start, "end": end},
        "missing_data": len(recovery_cases) == 0,
        "note": "Only confirmed recovered revenue (verified payment), not estimated potential.",
    }

    scorecard = {
        "currency": CURRENCY,
        "period": {"start": start, "end": end},
        "facility_id": facility_id,
        "department": department,
        "kpis": {
            "clean_claim_rate": clean_claim_data,
            "denial_rate": denial_data,
            "ar_days": ar_days_data,
            "collection_rate": collection_data,
            "charge_capture_rate": charge_capture_data,
            "claim_turnaround_time": turnaround_data,
            "denial_resolution_time": resolution_data,
            "revenue_recovery": recovery_data,
        },
        "summary": {
            "total_claims": len(claims),
            "submitted_claims": len(submitted),
            "approved_claims": len(approved),
            "denied_claims": len(rejected),
            "total_billed": total_billed,
            "total_collected": total_collected,
            "confirmed_recovery": revenue_recovery,
        },
        "data_quality": {
            k: v["missing_data"] for k, v in {
                "clean_claim_rate": clean_claim_data,
                "denial_rate": denial_data,
                "ar_days": ar_days_data,
                "collection_rate": collection_data,
                "charge_capture_rate": charge_capture_data,
                "claim_turnaround_time": turnaround_data,
                "denial_resolution_time": resolution_data,
                "revenue_recovery": recovery_data,
            }.items()
        },
    }

    return scorecard


# ===========================================================================
# Phase 5: Human Review & Accountability Enhancements
# ===========================================================================

def enhanced_review_tasks(filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Enhanced human review tasks with workload, priority, SLA, assignment history,
    escalation history, approval/rejection records, and audit trail."""
    c = get_db()
    f = filters or {}

    q = """SELECT rt.*, u.username as assigned_username, u.role as assigned_role
           FROM review_tasks rt
           LEFT JOIN users u ON rt.assigned_to=u.id
           WHERE 1=1"""
    params: List[Any] = []
    if f.get("status"):
        q += " AND rt.status=?"; params.append(f["status"])
    if f.get("priority"):
        q += " AND rt.priority=?"; params.append(f["priority"])
    if f.get("assigned_to"):
        q += " AND rt.assigned_to=?"; params.append(f["assigned_to"])
    q += " ORDER BY rt.created_at DESC"

    rows = c.execute(q, params).fetchall()
    tasks = []
    for r in rows:
        rd = dict(r)
        # SLA tracking: check if overdue based on due_date
        overdue = False
        sla_status = "on_track"
        if rd.get("due_date"):
            try:
                due = datetime.date.fromisoformat(rd["due_date"][:10])
                today = datetime.date.today()
                if today > due and rd.get("status") == "pending":
                    overdue = True
                    sla_status = "overdue"
                elif today > due:
                    sla_status = "past_due"
            except Exception:
                pass

        # Assignment history
        assignments = c.execute(
            """SELECT ra.*, ur.username as assigned_username
               FROM review_assignments ra
               LEFT JOIN user_roles ur ON ra.assigned_to=ur.username
               WHERE ra.review_task_id=?
               ORDER BY ra.assigned_at""",
            (rd["id"],),
        ).fetchall()

        # Escalation history
        escalations = c.execute(
            """SELECT * FROM review_task_escalations WHERE review_task_id=? ORDER BY escalated_at""",
            (rd["id"],),
        ).fetchall()

        tasks.append({
            "task_id": rd["id"],
            "task_type": rd.get("task_type"),
            "entity_type": rd.get("entity_type"),
            "entity_id": rd.get("entity_id"),
            "status": rd.get("status"),
            "priority": rd.get("priority", "medium"),
            "assigned_to": rd.get("assigned_to"),
            "assigned_username": rd.get("assigned_username"),
            "assigned_role": rd.get("assigned_role"),
            "reviewer": rd.get("reviewer"),
            "context": rd.get("context"),
            "evidence": rd.get("evidence"),
            "ai_recommendation": rd.get("ai_recommendation"),
            "human_decision": rd.get("human_decision"),
            "decision_timestamp": rd.get("decision_timestamp"),
            "financial_impact": _round2(rd.get("financial_impact")),
            "due_date": rd.get("due_date"),
            "overdue": overdue,
            "sla_status": sla_status,
            "created_at": rd.get("created_at"),
            "resolved_at": rd.get("resolved_at"),
            "assignment_history": [
                {
                    "assigned_to": a["assigned_to"],
                    "assigned_username": a["assigned_username"],
                    "assigned_by": a["assigned_by"],
                    "assigned_at": a["assigned_at"],
                    "unassigned_at": a["unassigned_at"],
                } for a in assignments
            ],
            "escalation_history": [
                {
                    "escalated_from": e["escalated_from"],
                    "escalated_to": e["escalated_to"],
                    "reason": e["reason"],
                    "escalated_at": e["escalated_at"],
                    "resolved_at": e["resolved_at"],
                } for e in escalations
            ],
            "audit_trail": _get_audit_for_entity(c, "review_task", rd["id"]),
        })

    # Reviewer workload
    reviewer_workload = _compute_reviewer_workload(c)

    c.close()
    return {
        "tasks": tasks,
        "count": len(tasks),
        "reviewer_workload": reviewer_workload,
        "currency": CURRENCY,
    }


def _compute_reviewer_workload(c) -> List[Dict[str, Any]]:
    """Compute current workload per reviewer from pending/assigned tasks."""
    rows = c.execute(
        """SELECT rt.assigned_to, ur.username, ur.role,
                  COUNT(*) as pending_count
           FROM review_tasks rt
           LEFT JOIN user_roles ur ON rt.assigned_to=ur.username
           WHERE rt.status='pending' AND rt.assigned_to IS NOT NULL
           GROUP BY rt.assigned_to
           ORDER BY pending_count DESC"""
    ).fetchall()
    workload = []
    for r in rows:
        rd = dict(r)
        assignee = rd["assigned_to"]
        overdue_count = c.execute(
            """SELECT COUNT(*) n FROM review_tasks
               WHERE assigned_to=? AND status='pending' AND due_date IS NOT NULL AND due_date < ?""",
            (assignee, _today_iso()),
        ).fetchone()["n"]
        financial_impact = c.execute(
            """SELECT COALESCE(SUM(financial_impact),0) s FROM review_tasks
               WHERE assigned_to=? AND status='pending'""",
            (assignee,),
        ).fetchone()["s"]
        workload.append({
            "reviewer": assignee,
            "reviewer_username": rd["username"],
            "reviewer_role": rd["role"],
            "pending_tasks": rd["pending_count"],
            "overdue_tasks": overdue_count,
            "total_financial_impact": _round2(financial_impact),
        })
    return workload


def resolve_review_task(task_id: str, decision: str, reviewer: str, notes: str = "",
                        human_decision: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a review task with full accountability tracking.

    Decisions: approve, reject, request_correction, assign, escalate, resolve
    Distinguishes AI recommendation, AI-prepared action, human-approved action, executed action.
    """
    valid_decisions = ("approve", "reject", "request_correction", "assign", "escalate", "resolve")
    if decision not in valid_decisions:
        return {"error": f"Invalid decision. Must be one of: {', '.join(valid_decisions)}"}, 400

    c = get_db()
    task = c.execute("SELECT * FROM review_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        c.close()
        return {"error": "Review task not found"}, 404

    td = dict(task)
    new_status = "resolved" if decision == "resolve" else decision
    now_ts = now()

    # Record human decision
    c.execute(
        """UPDATE review_tasks SET status=?, reviewer=?, human_decision=?, decision_timestamp=?, resolved_at=?
           WHERE id=?""",
        (new_status, reviewer, human_decision or decision, now_ts, now_ts, task_id),
    )

    # Audit trail
    audit(
        c, reviewer, "user", "Human Review", f"Review task {decision}d",
        "review_task", task_id,
        previous_state=td.get("status"), new_state=new_status,
        input_summary=json.dumps({"decision": decision, "notes": notes, "human_decision": human_decision}),
        output_summary=json.dumps({"reviewer": reviewer, "decision_timestamp": now_ts}),
        human_reviewer=reviewer,
        review_decision=human_decision or decision,
    )

    # If escalated, record escalation
    if decision == "escalate":
        escalate_to = notes  # notes field used as escalate target
        eid = str(uuid.uuid4())
        c.execute(
            """INSERT INTO review_task_escalations(id,review_task_id,escalated_from,escalated_to,reason,escalated_at)
               VALUES(?,?,?,?,?,?)""",
            (eid, task_id, td.get("assigned_to") or "unassigned", escalate_to, notes, now_ts),
        )
        # Re-assign
        c.execute(
            """UPDATE review_tasks SET assigned_to=? WHERE id=?""",
            (escalate_to, task_id),
        )

    c.commit()
    c.close()

    return {
        "task_id": task_id,
        "status": new_status,
        "decision": decision,
        "reviewer": reviewer,
        "human_decision": human_decision or decision,
        "decision_timestamp": now_ts,
        "ai_recommendation": td.get("ai_recommendation"),
        "distinction": {
            "ai_recommendation": td.get("ai_recommendation"),
            "human_decision": human_decision or decision,
            "note": "AI recommendation is advisory; human decision is the executed action.",
        },
    }


def _get_audit_for_entity(c, entity_type: str, entity_id: str) -> List[dict]:
    """Get audit trail entries for a specific entity."""
    rows = c.execute(
        """SELECT * FROM audit_trail WHERE entity_type=? AND entity_id=? ORDER BY timestamp""",
        (entity_type, entity_id),
    ).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# Phase 6: Revenue Impact Tracking Lifecycle
# ===========================================================================

# Valid lifecycle stages
IMPACT_STAGES = ["IDENTIFIED", "ESTIMATED", "ASSIGNED", "UNDER REVIEW", "ACTION TAKEN", "OUTCOME VERIFIED", "CLOSED"]

# Valid outcome categories
IMPACT_OUTCOMES = [
    "revenue_recovered", "revenue_protected", "claim_corrected", "charge_captured",
    "underpayment_resolved", "denial_overturned", "issue_rejected_invalid", "no_financial_recovery",
]


def create_revenue_impact(source_finding_id: str, source_finding_type: str,
                          original_issue: str, estimated_amount: float,
                          related_claim_id: Optional[str] = None,
                          related_encounter_id: Optional[str] = None,
                          related_charge_id: Optional[str] = None,
                          related_payment_id: Optional[str] = None,
                          assigned_owner: Optional[str] = None,
                          actor: str = "System",
                          evidence: Optional[str] = None) -> Dict[str, Any]:
    """Create a new revenue impact record at IDENTIFIED stage.

    Prevents duplicate impact attribution: checks if an impact already exists
    for the same source finding.
    """
    c = get_db()

    # Prevent duplicate impact attribution
    existing = c.execute(
        """SELECT id FROM revenue_impact_tracking
           WHERE source_finding_id=? AND source_finding_type=?
           ORDER BY created_at DESC LIMIT 1""",
        (source_finding_id, source_finding_type),
    ).fetchone()
    if existing:
        c.close()
        return {
            "error": "Duplicate impact attribution prevented",
            "existing_impact_id": existing["id"],
            "source_finding_id": source_finding_id,
            "message": "An impact record already exists for this source finding. Update the existing record instead.",
        }, 409

    impact_id = f"IMP-{str(uuid.uuid4())[:8].upper()}"
    rid = str(uuid.uuid4())
    now_ts = now()

    c.execute(
        """INSERT INTO revenue_impact_tracking(id, impact_id, source_finding_id, source_finding_type,
            related_claim_id, related_encounter_id, related_charge_id, related_payment_id,
            original_issue, lifecycle_stage, initial_estimated_amount,
            confirmed_recovered_amount, confirmed_protected_amount, outcome_category,
            assigned_owner, human_verification_status, supporting_evidence, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'IDENTIFIED',?,0,0,NULL,?,'unverified',?,?,?)""",
        (
            rid, impact_id, source_finding_id, source_finding_type,
            related_claim_id, related_encounter_id, related_charge_id, related_payment_id,
            original_issue, _round2(estimated_amount), assigned_owner,
            json.dumps(evidence) if evidence else None, now_ts, now_ts,
        ),
    )

    audit(
        c, actor, "user", "Revenue Impact Tracking", "Impact record created",
        "revenue_impact", impact_id,
        previous_state=None, new_state="IDENTIFIED",
        input_summary=json.dumps({"source_finding_id": source_finding_id, "estimated_amount": estimated_amount}),
        output_summary=json.dumps({"impact_id": impact_id, "stage": "IDENTIFIED"}),
    )

    c.commit()
    c.close()

    return {
        "impact_id": impact_id,
        "record_id": rid,
        "source_finding_id": source_finding_id,
        "source_finding_type": source_finding_type,
        "original_issue": original_issue,
        "lifecycle_stage": "IDENTIFIED",
        "initial_estimated_amount": _round2(estimated_amount),
        "confirmed_recovered_amount": 0.0,
        "confirmed_protected_amount": 0.0,
        "outcome_category": None,
        "assigned_owner": assigned_owner,
        "human_verification_status": "unverified",
        "related_claim_id": related_claim_id,
        "related_encounter_id": related_encounter_id,
        "currency": CURRENCY,
        "notice": "Estimated amount is not confirmed revenue. Confirmation requires verified payment records.",
    }


def update_revenue_impact_stage(impact_id: str, new_stage: str, actor: str = "User",
                                 notes: str = "") -> Dict[str, Any]:
    """Transition a revenue impact record through its lifecycle.

    Valid transitions enforce ordering: IDENTIFIED → ESTIMATED → ASSIGNED →
    UNDER REVIEW → ACTION TAKEN → OUTCOME VERIFIED → CLOSED
    """
    if new_stage not in IMPACT_STAGES:
        return {"error": f"Invalid stage. Must be one of: {', '.join(IMPACT_STAGES)}"}, 400

    c = get_db()
    row = c.execute("SELECT * FROM revenue_impact_tracking WHERE impact_id=?", (impact_id,)).fetchone()
    if not row:
        c.close()
        return {"error": "Revenue impact record not found"}, 404

    rd = dict(row)
    current_stage = rd.get("lifecycle_stage")

    # Validate transition ordering
    current_idx = IMPACT_STAGES.index(current_stage) if current_stage in IMPACT_STAGES else -1
    new_idx = IMPACT_STAGES.index(new_stage)
    if new_idx <= current_idx and new_stage != current_stage:
        c.close()
        return {"error": f"Cannot transition from {current_stage} to {new_stage}. Stages must progress forward."}, 400

    now_ts = now()
    c.execute(
        """UPDATE revenue_impact_tracking SET lifecycle_stage=?, updated_at=? WHERE impact_id=?""",
        (new_stage, now_ts, impact_id),
    )

    audit(
        c, actor, "user", "Revenue Impact Tracking", f"Impact stage transitioned to {new_stage}",
        "revenue_impact", impact_id,
        previous_state=current_stage, new_state=new_stage,
        input_summary=json.dumps({"notes": notes}),
        output_summary=json.dumps({"stage": new_stage}),
    )

    c.commit()
    c.close()

    return {
        "impact_id": impact_id,
        "previous_stage": current_stage,
        "current_stage": new_stage,
        "transitioned_by": actor,
        "transitioned_at": now_ts,
        "currency": CURRENCY,
    }


def record_revenue_impact_outcome(impact_id: str, outcome_category: str,
                                   confirmed_amount: float, actor: str = "User",
                                   verified_by: Optional[str] = None,
                                   evidence: Optional[str] = None) -> Dict[str, Any]:
    """Record the outcome of a revenue impact record.

    Distinguishes confirmed recovered vs confirmed protected revenue.
    Prevents double-counting: same financial outcome cannot be attributed to
    multiple impact records for the same related claim.

    outcome_category: revenue_recovered, revenue_protected, claim_corrected,
                      charge_captured, underpayment_resolved, denial_overturned,
                      issue_rejected_invalid, no_financial_recovery
    """
    if outcome_category not in IMPACT_OUTCOMES:
        return {"error": f"Invalid outcome. Must be one of: {', '.join(IMPACT_OUTCOMES)}"}, 400

    c = get_db()
    row = c.execute("SELECT * FROM revenue_impact_tracking WHERE impact_id=?", (impact_id,)).fetchone()
    if not row:
        c.close()
        return {"error": "Revenue impact record not found"}, 404

    rd = dict(row)
    confirmed_amount = _round2(confirmed_amount)
    now_ts = now()

    # Prevent double-counting: check if this claim already has a confirmed outcome
    # of the same type (recovered or protected)
    if outcome_category in ("revenue_recovered", "revenue_protected") and rd.get("related_claim_id"):
        existing = c.execute(
            """SELECT id FROM revenue_impact_tracking
               WHERE related_claim_id=? AND outcome_category=? AND confirmed_recovered_amount > 0
               AND impact_id<>?
               LIMIT 1""",
            (rd["related_claim_id"], outcome_category, impact_id),
        ).fetchone()
        if existing:
            c.close()
            return {
                "error": f"Duplicate {outcome_category} attribution prevented",
                "existing_impact_id": existing["id"],
                "related_claim_id": rd["related_claim_id"],
                "message": f"{outcome_category} already confirmed for this claim via another impact record.",
            }, 409

    # Update based on outcome category
    new_recovered = _money(rd.get("confirmed_recovered_amount"))
    new_protected = _money(rd.get("confirmed_protected_amount"))

    if outcome_category == "revenue_recovered":
        new_recovered = _round2(new_recovered + confirmed_amount)
    elif outcome_category == "revenue_protected":
        new_protected = _round2(new_protected + confirmed_amount)

    c.execute(
        """UPDATE revenue_impact_tracking SET outcome_category=?, confirmed_recovered_amount=?,
               confirmed_protected_amount=?, lifecycle_stage='OUTCOME VERIFIED',
               human_verification_status='verified', human_verifier=?,
               verification_date=?, resolution_date=?, supporting_evidence=?,
               updated_at=? WHERE impact_id=?""",
        (
            outcome_category, new_recovered, new_protected,
            verified_by or actor, now_ts, now_ts,
            json.dumps(evidence) if evidence else None, now_ts, impact_id,
        ),
    )

    audit(
        c, actor, "user", "Revenue Impact Tracking", f"Outcome recorded: {outcome_category}",
        "revenue_impact", impact_id,
        previous_state=rd.get("lifecycle_stage"), new_state="OUTCOME VERIFIED",
        input_summary=json.dumps({"outcome": outcome_category, "amount": confirmed_amount}),
        output_summary=json.dumps({"confirmed_recovered": new_recovered, "confirmed_protected": new_protected}),
        human_reviewer=verified_by or actor,
        review_decision=outcome_category,
    )

    c.commit()
    c.close()

    return {
        "impact_id": impact_id,
        "outcome_category": outcome_category,
        "confirmed_amount": confirmed_amount,
        "total_confirmed_recovered": new_recovered,
        "total_confirmed_protected": new_protected,
        "lifecycle_stage": "OUTCOME VERIFIED",
        "human_verification_status": "verified",
        "verified_by": verified_by or actor,
        "verification_date": now_ts,
        "currency": CURRENCY,
        "distinction": {
            "recovered": new_recovered,
            "protected": new_protected,
            "note": "Confirmed recovery requires actual payment records; protected revenue is prevented loss, not received cash.",
        },
    }


def close_revenue_impact(impact_id: str, actor: str = "User",
                          notes: str = "") -> Dict[str, Any]:
    """Close a revenue impact record after outcome verification."""
    c = get_db()
    row = c.execute("SELECT * FROM revenue_impact_tracking WHERE impact_id=?", (impact_id,)).fetchone()
    if not row:
        c.close()
        return {"error": "Revenue impact record not found"}, 404

    rd = dict(row)
    if rd.get("lifecycle_stage") not in ("OUTCOME VERIFIED",):
        # Allow closing from any stage if explicitly requested, but warn
        pass

    now_ts = now()
    c.execute(
        """UPDATE revenue_impact_tracking SET lifecycle_stage='CLOSED', updated_at=? WHERE impact_id=?""",
        (now_ts, impact_id),
    )

    audit(
        c, actor, "user", "Revenue Impact Tracking", "Impact record closed",
        "revenue_impact", impact_id,
        previous_state=rd.get("lifecycle_stage"), new_state="CLOSED",
        input_summary=json.dumps({"notes": notes}),
        output_summary=json.dumps({"stage": "CLOSED"}),
    )

    c.commit()
    c.close()

    return {
        "impact_id": impact_id,
        "previous_stage": rd.get("lifecycle_stage"),
        "current_stage": "CLOSED",
        "closed_by": actor,
        "closed_at": now_ts,
        "currency": CURRENCY,
    }


def list_revenue_impacts(filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """List revenue impact records with optional filtering."""
    c = get_db()
    f = filters or {}

    q = """SELECT * FROM revenue_impact_tracking WHERE 1=1"""
    params: List[Any] = []
    if f.get("lifecycle_stage"):
        q += " AND lifecycle_stage=?"; params.append(f["lifecycle_stage"])
    if f.get("outcome_category"):
        q += " AND outcome_category=?"; params.append(f["outcome_category"])
    if f.get("related_claim_id"):
        q += " AND related_claim_id=?"; params.append(f["related_claim_id"])
    if f.get("source_finding_type"):
        q += " AND source_finding_type=?"; params.append(f["source_finding_type"])
    q += " ORDER BY created_at DESC"

    rows = c.execute(q, params).fetchall()
    impacts = []
    for r in rows:
        rd = dict(r)
        impacts.append({
            "impact_id": rd["impact_id"],
            "record_id": rd["id"],
            "source_finding_id": rd.get("source_finding_id"),
            "source_finding_type": rd.get("source_finding_type"),
            "related_claim_id": rd.get("related_claim_id"),
            "related_encounter_id": rd.get("related_encounter_id"),
            "related_charge_id": rd.get("related_charge_id"),
            "related_payment_id": rd.get("related_payment_id"),
            "original_issue": rd.get("original_issue"),
            "lifecycle_stage": rd.get("lifecycle_stage"),
            "initial_estimated_amount": _round2(rd.get("initial_estimated_amount")),
            "confirmed_recovered_amount": _round2(rd.get("confirmed_recovered_amount")),
            "confirmed_protected_amount": _round2(rd.get("confirmed_protected_amount")),
            "outcome_category": rd.get("outcome_category"),
            "assigned_owner": rd.get("assigned_owner"),
            "human_verification_status": rd.get("human_verification_status"),
            "human_verifier": rd.get("human_verifier"),
            "verification_date": rd.get("verification_date"),
            "resolution_date": rd.get("resolution_date"),
            "supporting_evidence": rd.get("supporting_evidence"),
            "created_at": rd.get("created_at"),
            "updated_at": rd.get("updated_at"),
            "currency": CURRENCY,
        })

    total_estimated = _round2(sum(i["initial_estimated_amount"] for i in impacts))
    total_confirmed_recovered = _round2(sum(i["confirmed_recovered_amount"] for i in impacts))
    total_confirmed_protected = _round2(sum(i["confirmed_protected_amount"] for i in impacts))

    c.close()
    return {
        "currency": CURRENCY,
        "impacts": impacts,
        "count": len(impacts),
        "totals": {
            "total_estimated": total_estimated,
            "total_confirmed_recovered": total_confirmed_recovered,
            "total_confirmed_protected": total_confirmed_protected,
        },
        "distinctions": {
            "estimated_vs_confirmed": "Initial estimated amount is not confirmed revenue",
            "recovered_vs_protected": "Confirmed recovered is actual payment; confirmed protected is prevented loss",
        },
    }


# ===========================================================================
# Phase 8: Agent & Orchestrator Integration
# ===========================================================================

def integrate_leakage_with_orchestrator(c, leakage_case: dict, actor: str = "System") -> dict:
    """Create an orchestrator task from a revenue leakage finding.

    Routes to the Billing Agent for charge-related issues, or the
    Reconciliation Agent for payment-related issues.
    """
    from agents import orchestrator as orch_mod

    category = (leakage_case.get("category") or "").lower()
    amount = _money(leakage_case.get("amount"))

    # Route to appropriate agent based on category
    if "duplicate" in category or "charge" in category or "billing" in category:
        agent_name = "Billing Agent"
        task_type = "leakage_review"
        direction = "agent_to_agent"
    elif "underpayment" in category or "payment" in category:
        agent_name = "Payment and Reconciliation Agent"
        task_type = "leakage_reconciliation"
        direction = "agent_to_agent"
    else:
        agent_name = "Revenue Leakage Detection Agent"
        task_type = "leakage_investigation"
        direction = "agent_to_human"

    entity_id = leakage_case.get("claim_id") or leakage_case.get("encounter_id") or leakage_case["id"]
    entity_type = "claim" if leakage_case.get("claim_id") else "encounter"

    result = orch_mod.create_orchestrator_task(
        c, task_type, entity_type, entity_id,
        agent_name=agent_name, direction=direction,
        priority="high" if amount >= 500 else "medium",
        context=f"Revenue leakage: {leakage_case.get('category')} - {leakage_case.get('entity_id')}",
        evidence=leakage_case.get("evidence", ""),
        ai_recommendation=leakage_case.get("recommended_action", ""),
        financial_impact=amount,
        auto_assign=True,
    )

    # Update agent states
    orch_mod.set_agent_state(agent_name, "Assigned", task_type,
                             {"source": "leakage_finding", "case_id": leakage_case["id"]})

    return {
        "orchestrator_task": result,
        "assigned_agent": agent_name,
        "direction": direction,
        "financial_impact": _round2(amount),
    }


def integrate_revenue_impact_with_review(impact_id: str, actor: str = "User") -> dict:
    """Create a human review task from a revenue impact record at ESTIMATED stage.

    Routes to the Human Review & Escalation workflow for verification.
    """
    c = get_db()
    row = c.execute("SELECT * FROM revenue_impact_tracking WHERE impact_id=?", (impact_id,)).fetchone()
    if not row:
        c.close()
        return {"error": "Revenue impact record not found"}, 404

    rd = dict(row)
    if rd.get("lifecycle_stage") != "ESTIMATED":
        c.close()
        return {"error": "Impact record must be at ESTIMATED stage for review"}, 400

    # Create review task
    tid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO review_tasks(id,task_type,entity_type,entity_id,status,priority,assigned_to,context,evidence,ai_recommendation,financial_impact,due_date,created_at)
        VALUES(?,?,'revenue_impact',?,'pending','high',NULL,?,?,?,?,?,?)""",
        (
            tid, "revenue_impact_review", impact_id,
            f"Review revenue impact: {rd.get('original_issue')}",
            json.dumps({"impact_id": impact_id, "estimated_amount": rd.get("initial_estimated_amount")}),
            rd.get("ai_recommendation") or "Verify estimated revenue impact and determine outcome",
            _round2(rd.get("initial_estimated_amount")),
            None, now(),
        ),
    )
    c.commit()
    c.close()

    return {
        "review_task_id": tid,
        "impact_id": impact_id,
        "task_type": "revenue_impact_review",
        "status": "pending",
        "financial_impact": _round2(rd.get("initial_estimated_amount")),
    }


def integrate_denial_with_impact(denial_case: dict, actor: str = "User") -> dict:
    """Create a revenue impact record from a denied claim.

    Links the denial finding to the revenue impact tracking lifecycle.
    """
    claim_id = denial_case.get("claim_id")
    if not claim_id:
        return {"error": "Denial case missing claim_id"}, 400

    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not claim:
        c.close()
        return {"error": "Claim not found"}, 404
    cd = dict(claim)

    amount = _money(cd.get("total_charges"))
    original_issue = f"Claim {cd.get('claim_number')} denied: {denial_case.get('denial_reason') or 'denied'}"

    # Create impact record
    impact_result = create_revenue_impact(
        source_finding_id=denial_case.get("id", claim_id),
        source_finding_type="denial",
        original_issue=original_issue,
        estimated_amount=amount,
        related_claim_id=claim_id,
        related_encounter_id=cd.get("encounter_id"),
        assigned_owner="Claims Officer",
        actor=actor,
        evidence=json.dumps({
            "denial_reason": denial_case.get("denial_reason"),
            "denial_category": denial_case.get("denial_category"),
            "claim_status": cd.get("status"),
        }),
    )
    if isinstance(impact_result, tuple):
        c.close()
        return impact_result

    c.close()
    return {
        "impact_id": impact_result["impact_id"],
        "claim_id": claim_id,
        "claim_number": cd.get("claim_number"),
        "estimated_amount": impact_result["initial_estimated_amount"],
        "lifecycle_stage": "IDENTIFIED",
        "source": "denial",
    }


def integrate_underpayment_with_impact(underpayment_case: dict, actor: str = "User") -> dict:
    """Create a revenue impact record from an underpayment case."""
    claim_id = underpayment_case.get("claim_id")
    if not claim_id:
        return {"error": "Underpayment case missing claim_id"}, 400

    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not claim:
        c.close()
        return {"error": "Claim not found"}, 404
    cd = dict(claim)

    outstanding = _money(underpayment_case.get("outstanding_amount"))
    original_issue = f"Claim {cd.get('claim_number')} underpaid: outstanding {outstanding:.2f} {CURRENCY}"

    impact_result = create_revenue_impact(
        source_finding_id=underpayment_case.get("id", claim_id),
        source_finding_type="underpayment",
        original_issue=original_issue,
        estimated_amount=outstanding,
        related_claim_id=claim_id,
        related_encounter_id=cd.get("encounter_id"),
        assigned_owner="Reconciliation Agent",
        actor=actor,
        evidence=json.dumps({
            "expected_reimbursement": underpayment_case.get("expected_reimbursement"),
            "reconciled_payment": underpayment_case.get("reconciled_payment"),
            "outstanding_amount": outstanding,
            "discrepancy_reason": underpayment_case.get("discrepancy_reason"),
        }),
    )
    if isinstance(impact_result, tuple):
        c.close()
        return impact_result

    c.close()
    return {
        "impact_id": impact_result["impact_id"],
        "claim_id": claim_id,
        "claim_number": cd.get("claim_number"),
        "estimated_amount": impact_result["initial_estimated_amount"],
        "lifecycle_stage": "IDENTIFIED",
        "source": "underpayment",
    }


def run_integrated_revenue_integrity(actor: str = "System") -> dict:
    """Run a coordinated revenue integrity check across all agents.

    Coordinates:
    1. Service-to-revenue completeness (Revenue Integrity Agent)
    2. Closed-claim leakage detection (Revenue Integrity Agent)
    3. Cross-department gap detection (Revenue Integrity Agent)
    4. Underpayment detection (Reconciliation Agent)
    5. Denial detection (Denial Management Agent)

    Creates orchestrator tasks for each finding.
    """
    from agents import revenue_integrity as ri_mod
    from agents import reconciliation as recon_mod
    from agents import denial as denial_mod

    results = {}

    # 1. Service-to-revenue completeness for all encounters
    c = get_db()
    encounters = [dict(r) for r in c.execute("SELECT * FROM encounters").fetchall()]
    c.close()

    service_results = []
    for enc in encounters:
        sr = ri_mod.check_service_to_revenue_completeness(enc["id"], actor)
        if not isinstance(sr, tuple) and sr.get("cases_created", 0) > 0:
            service_results.append(sr)
    results["service_to_revenue"] = service_results

    # 2. Closed-claim leakage detection
    leak_result = ri_mod.detect_closed_claim_leakage(None, actor)
    if not isinstance(leak_result, tuple):
        results["closed_claim_leakage"] = leak_result

    # 3. Cross-department gaps
    gap_result = ri_mod.detect_cross_department_gaps(actor)
    if not isinstance(gap_result, tuple):
        results["cross_department_gaps"] = gap_result

    # 4. Underpayment detection
    under_result = recon_mod.detect_underpayments(actor)
    if not isinstance(under_result, tuple):
        results["underpayments"] = under_result

    # 5. Denial detection
    denial_result = denial_mod.detect_denials()
    if not isinstance(denial_result, tuple):
        results["denials"] = denial_result

    # Create orchestrator tasks for high-priority findings
    c = get_db()
    tasks_created = 0
    for sr in service_results:
        for case in sr.get("cases_created", []):
            task = integrate_leakage_with_orchestrator(c, case, actor)
            tasks_created += 1
    c.close()

    results["orchestrator_tasks_created"] = tasks_created
    results["currency"] = CURRENCY
    results["notice"] = "Coordinated revenue integrity check using existing agents only."

    return results