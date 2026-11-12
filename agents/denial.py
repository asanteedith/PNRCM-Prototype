"""Denial Management Agent.

Detects denied and rejected claims, categorizes denial reasons, suggests
corrective actions, tracks correction and resubmission status, maintains
denial history, and identifies recurring denial patterns.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any, Dict, List

from db import get_db, now, audit

DENIAL_CATEGORIES = {
    "missing_documentation": "Missing or incomplete documentation",
    "coding_error": "Coding error or invalid code",
    "eligibility": "Patient eligibility or coverage issue",
    "duplicate_claim": "Duplicate claim",
    "missing_charge": "Missing charge or unbilled service",
    "timely_filing": "Claim submitted outside timely filing window",
    "authorization": "Missing prior authorization",
    "coordination_of_benefits": "Coordination of benefits issue",
    "other": "Other denial reason",
}

CORRECTIVE_ACTIONS = {
    "missing_documentation": "Obtain and attach missing documentation; resubmit claim.",
    "coding_error": "Review and correct diagnosis/procedure codes; resubmit claim.",
    "eligibility": "Verify patient eligibility and coverage; update subscriber information.",
    "duplicate_claim": "Withdraw duplicate; follow up on the original claim.",
    "missing_charge": "Add missing charges to the claim; resubmit.",
    "timely_filing": "Submit appeal with proof of timely filing if applicable.",
    "authorization": "Obtain prior authorization; resubmit claim with authorization.",
    "coordination_of_benefits": "Coordinate with primary payer; resubmit with EOB.",
    "other": "Review denial reason and take appropriate corrective action.",
}

DEFAULT_DENIAL_REASONS = [
    ("missing_documentation", "Documentation missing to support services billed"),
    ("coding_error", "Invalid or inconsistent diagnosis/procedure code"),
    ("eligibility", "Patient not eligible for service on date of service"),
    ("duplicate_claim", "Duplicate claim for same encounter and payer"),
    ("missing_charge", "Claim total lower than billed services"),
    ("timely_filing", "Claim submitted after payer filing deadline"),
    ("authorization", "Prior authorization not obtained"),
    ("coordination_of_benefits", "Primary payer EOB not attached"),
]


def _categorize(reason: str) -> str:
    r = (reason or "").strip().lower().replace("_", " ")
    for key, label in DENIAL_CATEGORIES.items():
        if key == "other":
            continue
        key_words = key.replace("_", " ")
        if key_words in r or r in label.lower():
            return key
    return "other"


def detect_denials() -> Dict[str, Any]:
    """Scan all claims and build denial records for denied/rejected claims."""
    c = get_db()
    claims = c.execute(
        "SELECT * FROM claims WHERE status IN ('denied','rejected') ORDER BY updated_at DESC"
    ).fetchall()
    denial_records: List[Dict[str, Any]] = []
    for cl in claims:
        cl_dict = dict(cl)
        existing = c.execute(
            "SELECT id FROM denials WHERE claim_id=?", (cl_dict["id"],)
        ).fetchone()
        if existing:
            continue
        category = _categorize(cl_dict.get("denial_reason") or "")
        corrective = CORRECTIVE_ACTIONS.get(category, CORRECTIVE_ACTIONS["other"])
        did = str(uuid.uuid4())
        c.execute(
            """INSERT INTO denials(id, claim_id, denial_date, denial_reason, denial_category, corrective_action, status, resubmission_count, resolved_at, created_at)
            VALUES(?,?,?,?,?,?,'open',0,NULL,?)""",
            (did, cl_dict["id"], cl_dict.get("updated_at") or now(),
             cl_dict.get("denial_reason"), category, corrective, now()),
        )
        audit(
            c,
            "Denial Management Agent",
            "agent",
            "Denial Management",
            "Denial detected and recorded",
            "claim",
            cl_dict["id"],
            previous_state=cl_dict["status"],
            new_state="denied_recorded",
            recommendation=category,
            decision=None,
        )
        denial_records.append({
            "denial_id": did,
            "claim_id": cl_dict["id"],
            "claim_number": cl_dict["claim_number"],
            "denial_reason": cl_dict.get("denial_reason"),
            "denial_category": category,
            "category_label": DENIAL_CATEGORIES.get(category, "Other"),
            "corrective_action": corrective,
            "status": "open",
            "resubmission_count": 0,
        })
    c.commit()
    c.close()
    return denial_records


def list_denials() -> Dict[str, Any]:
    c = get_db()
    rows = c.execute(
        """SELECT d.*, c.claim_number, c.total_charges, c.patient_id, c.encounter_id,
                  c.payer_name, c.payer_type, c.denial_reason AS claim_denial_reason,
                  c.denial_category AS claim_denial_category
        FROM denials d JOIN claims c ON d.claim_id=c.id
        ORDER BY d.created_at DESC"""
    ).fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        # Frontend expects payer_name, amount, deadline
        d["payer_name"] = d.get("payer_name") or d.get("payer_type") or "Unknown"
        d["amount"] = d.get("total_charges") or 0
        d["deadline"] = d.get("deadline") or d.get("denial_date")
        out.append(d)
    return out


def resolve_denial(denial_id: str, decision: str, corrective_action: str = "", resubmit: bool = False) -> Dict[str, Any]:
    """Human reviewer resolves a denial.

    decision: 'corrected' | 'resubmitted' | 'appealed' | 'closed'
    """
    if decision not in ("corrected", "resubmitted", "appealed", "closed"):
        return {"error": "decision must be corrected, resubmitted, appealed, or closed"}, 400
    c = get_db()
    den = c.execute("SELECT * FROM denials WHERE id=?", (denial_id,)).fetchone()
    if not den:
        c.close()
        return {"error": "Denial not found"}, 404
    den_dict = dict(den)
    if den_dict["status"] in ("closed",):
        c.close()
        return {"error": "Denial already closed"}, 409

    new_status = den_dict["status"]
    resub_count = den_dict["resubmission_count"] or 0
    if decision == "corrected":
        new_status = "corrected"
    elif decision == "resubmitted":
        new_status = "resubmitted"
        resub_count += 1
        claim = c.execute("SELECT * FROM claims WHERE id=?", (den_dict["claim_id"],)).fetchone()
        if claim:
            c.execute(
                "UPDATE claims SET status='resubmitted', updated_at=? WHERE id=?",
                (now(), claim["id"]),
            )
    elif decision == "appealed":
        new_status = "appealed"
    elif decision == "closed":
        new_status = "closed"

    resolved = now() if new_status in ("closed", "corrected") else None
    c.execute(
        """UPDATE denials SET status=?, corrective_action=COALESCE(?, corrective_action), resubmission_count=?, resolved_at=? WHERE id=?""",
        (new_status, corrective_action or None, resub_count, resolved, denial_id),
    )
    audit(
        c,
        "Human Reviewer",
        "user",
        "Denial Management",
        f"Denial {decision}",
        "denial",
        denial_id,
        previous_state=den_dict["status"],
        new_state=new_status,
        decision=decision,
    )
    c.commit()
    c.close()
    return {
        "denial_id": denial_id,
        "status": new_status,
        "decision": decision,
        "resubmission_count": resub_count,
    }


def denial_patterns() -> Dict[str, Any]:
    """Identify recurring denial patterns from denial records."""
    c = get_db()
    rows = c.execute(
        """SELECT d.denial_category, d.denial_reason, d.status, c.claim_number, c.total_charges, c.payer_type
        FROM denials d JOIN claims c ON d.claim_id=c.id"""
    ).fetchall()
    c.close()
    category_counts: Dict[str, int] = {}
    reason_counts: Dict[str, int] = {}
    status_counts: Dict[str, int] = {}
    payer_counts: Dict[str, int] = {}
    total_amount = 0.0
    for r in rows:
        cat = r["denial_category"] or "uncategorized"
        category_counts[cat] = category_counts.get(cat, 0) + 1
        reason = r["denial_reason"] or "not documented"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        st = r["status"] or "unknown"
        status_counts[st] = status_counts.get(st, 0) + 1
        payer = r["payer_type"] or "unknown"
        payer_counts[payer] = payer_counts.get(payer, 0) + 1
        total_amount += float(r["total_charges"] or 0)

    top_categories = sorted(category_counts.items(), key=lambda x: x[1], reverse=True)
    top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)

    return {
        "total_denials": len(rows),
        "total_denied_amount": round(total_amount, 2),
        "by_category": [{"category": k, "label": DENIAL_CATEGORIES.get(k, k), "count": v} for k, v in top_categories],
        "top_reasons": [{"reason": k, "count": v} for k, v in top_reasons[:10]],
        "by_status": status_counts,
        "by_payer": payer_counts,
        "recurring_pattern": top_categories[0][0] if top_categories else None,
    }
