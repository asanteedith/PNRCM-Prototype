"""Revenue Leakage Detection & Resolution Agent.

Identifies possible revenue leakage such as unbilled services, missing charges,
duplicate/inconsistent charges, underbilling, unresolved rejected claims,
claims not followed up, unreconciled payments, documentation gaps, claim
preparation errors, duplicate billing risk, and outstanding revenue.

Suspected leakage is NOT labeled as confirmed fraud. Human review is required
before any financial action.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from db import get_db, now, audit, add_exception
import agents.orchestrator as orch

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

CATEGORY_MAP = {
    "unbilled_service": "Services provided but not billed",
    "missing_charge": "Missing charge",
    "duplicate_charge": "Duplicate or inconsistent charge",
    "underbilling": "Underbilling",
    "rejected_claim_unresolved": "Unresolved rejected claim",
    "claim_not_followed_up": "Claim not followed up",
    "unreconciled_payment": "Unreconciled payment",
    "billed_vs_reimbursed": "Difference between billed and reimbursed",
    "documentation_gap": "Documentation gap",
    "claim_preparation_errors": "Claim preparation errors",
    "outstanding_revenue": "Outstanding revenue",
    "duplicate_billing_risk": "Duplicate billing risk",
}

SEVERITY_MAP = {
    "unbilled_service": "medium",
    "missing_charge": "high",
    "duplicate_charge": "high",
    "underbilling": "medium",
    "rejected_claim_unresolved": "high",
    "claim_not_followed_up": "medium",
    "unreconciled_payment": "high",
    "billed_vs_reimbursed": "critical",
    "documentation_gap": "medium",
    "claim_preparation_errors": "high",
    "outstanding_revenue": "medium",
    "duplicate_billing_risk": "high",
}

PRIORITY_MAP = {
    "critical": "high",
    "high": "high",
    "medium": "medium",
    "low": "low",
}

# Valid lifecycle transitions
VALID_TRANSITIONS = {
    "DETECTED": ["UNDER_REVIEW", "DISMISSED", "FALSE_POSITIVE"],
    "UNDER_REVIEW": ["CONFIRMED", "DISMISSED", "FALSE_POSITIVE"],
    "CONFIRMED": ["ACTION_REQUIRED", "DISMISSED", "FALSE_POSITIVE"],
    "ACTION_REQUIRED": ["IN_PROGRESS"],
    "IN_PROGRESS": ["RESOLVED", "REOPENED"],
    "RESOLVED": ["REOPENED"],
    "REOPENED": ["UNDER_REVIEW"],
    "DISMISSED": [],
    "FALSE_POSITIVE": [],
}

# Terminal states
TERMINAL_STATES = {"RESOLVED", "DISMISSED", "FALSE_POSITIVE"}

# Confirmed states (leakage is confirmed, not just suspected)
CONFIRMED_STATES = {"CONFIRMED", "ACTION_REQUIRED", "IN_PROGRESS", "RESOLVED"}


def _severity_for(category: str) -> str:
    return SEVERITY_MAP.get(category, "medium")


def _priority_for(category: str) -> str:
    return PRIORITY_MAP.get(SEVERITY_MAP.get(category, "medium"), "medium")


def _existing_case(c, entity_type: str, entity_id: str, category: str) -> Optional[Dict[str, Any]]:
    """Check if a leakage case already exists for this entity/category."""
    row = c.execute(
        "SELECT * FROM leakage_cases WHERE entity_type=? AND entity_id=? AND category=? AND status IN ('DETECTED','UNDER_REVIEW','CONFIRMED','ACTION_REQUIRED','IN_PROGRESS')",
        (entity_type, entity_id, category),
    ).fetchone()
    return dict(row) if row else None


def _persist_case(c, item: Dict[str, Any]) -> tuple:
    """Insert or update a leakage case in leakage_cases.

    Returns (case_id, created) where created is True only when a new case row
    was inserted (used to avoid creating duplicate orchestrator tasks on repeat
    detection runs).
    """
    existing = _existing_case(c, item["entity_type"], item["entity_id"], item["category"])
    if existing:
        c.execute(
            """UPDATE leakage_cases
               SET estimated_exposure=?, amount=?, supporting_evidence=?, recommended_action=?, updated_at=?,
                   description=COALESCE(?, description)
             WHERE id=?""",
            (
                item["estimated_exposure"],
                item["estimated_exposure"],
                item["supporting_evidence"],
                item["recommended_action"],
                now(),
                item.get("description"),
                existing["id"],
            ),
        )
        return existing["id"], False
    cid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO leakage_cases(id, facility_id, category, description, detection_source,
            detection_rule, supporting_evidence, amount, estimated_exposure, confirmed_amount,
            verified_recovered_amount, priority, status, assigned_user, recommended_action,
            resolution_notes, false_positive_reason, entity_type, entity_id, patient_id,
            encounter_id, claim_id, created_at, updated_at, resolved_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            cid,                                    # id
            item.get("facility_id"),                # facility_id
            item["category"],                       # category
            item.get("description", ""),            # description
            item.get("detection_source", "system_scan"),  # detection_source
            item.get("detection_rule", ""),         # detection_rule
            item["supporting_evidence"],            # supporting_evidence
            item["estimated_exposure"],             # amount (legacy, kept in sync)
            item["estimated_exposure"],             # estimated_exposure
            0.0,                                    # confirmed_amount
            0.0,                                    # verified_recovered_amount
            item["priority"],                       # priority
            "DETECTED",                             # status
            None,                                   # assigned_user
            item.get("description", ""),            # recommended_action
            None,                                   # resolution_notes
            None,                                   # false_positive_reason
            item["entity_type"],                    # entity_type
            item["entity_id"],                      # entity_id
            item.get("patient_id"),                 # patient_id
            item.get("encounter_id"),               # encounter_id
            item.get("claim_id"),                   # claim_id
            now(),                                  # created_at
            now(),                                  # updated_at
            None,                                   # resolved_at
        ),
    )
    return cid, True


def _transition_case(case_id: str, new_status: str, actor: str,
                     reason: Optional[str] = None) -> Dict[str, Any]:
    """Transition a leakage case to a new status, validating the transition."""
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return {"error": "Leakage case not found"}, 404
    current = row["status"]
    allowed = VALID_TRANSITIONS.get(current, [])
    if new_status not in allowed:
        c.close()
        return {
            "error": f"Invalid transition from '{current}' to '{new_status}'. Allowed: {', '.join(allowed) or 'none'}",
        }, 400

    resolved_at = row["resolved_at"]
    if new_status in TERMINAL_STATES:
        resolved_at = now()
    elif new_status == "REOPENED":
        resolved_at = None

    c.execute(
        """UPDATE leakage_cases SET status=?, updated_at=?, resolved_at=? WHERE id=?""",
        (new_status, now(), resolved_at, case_id),
    )
    audit(
        c, actor, "user", "Revenue Leakage Detection Agent",
        f"Leakage case transition: {current} -> {new_status}",
        "leakage_case", case_id,
        previous_state=json.dumps({"status": current}),
        new_state=json.dumps({"status": new_status, "reason": reason or ""}),
        input_summary=json.dumps({"transition": f"{current}->{new_status}", "actor": actor, "reason": reason or ""}),
    )

    # Link orchestrator task if exists
    task = c.execute(
        "SELECT id FROM orchestrator_tasks WHERE entity_id=? AND entity_type='leakage_case' AND task_type='leakage_review' ORDER BY created_at DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if task:
        c.execute(
            "UPDATE orchestrator_tasks SET status=? WHERE id=?",
            ("completed" if new_status in TERMINAL_STATES else "started", task["id"]),
        )

    c.commit(); c.close()
    return {"ok": True, "case_id": case_id, "previous_status": current, "status": new_status}


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

def _detect_unbilled_services(c) -> List[Dict[str, Any]]:
    """A. Unbilled services: services delivered but not billed."""
    items = []
    rows = c.execute(
        """SELECT s.id, s.service_name, s.total_price, s.encounter_id,
                  e.encounter_id as enc_no, e.patient_id
           FROM services s JOIN encounters e ON s.encounter_id=e.id
           WHERE s.billed=0"""
    ).fetchall()
    for row in rows:
        amt = float(row["total_price"] or 0)
        items.append({
            "category": "unbilled_service",
            "category_label": CATEGORY_MAP["unbilled_service"],
            "estimated_exposure": amt,
            "entity_type": "service",
            "entity_id": row["id"],
            "encounter_id": row["enc_no"],
            "patient_id": row["patient_id"],
            "detection_source": "system_scan",
            "detection_rule": "unbilled_service: services.billed=0",
            "supporting_evidence": f"Service '{row['service_name']}' delivered but not billed (billed=0). Amount: GHS {amt:.2f}.",
            "recommended_action": "Add the service to a bill or mark as non-billable.",
            "priority": _priority_for("unbilled_service"),
            "severity": _severity_for("unbilled_service"),
        })
    return items


def _detect_missing_charges(c) -> List[Dict[str, Any]]:
    """B. Missing charges: claim total < sum of billed services."""
    items = []
    claims = c.execute("SELECT * FROM claims WHERE status IN ('submitted','vetted','paid')").fetchall()
    for cl in claims:
        cl_dict = dict(cl)
        enc = c.execute("SELECT id FROM encounters WHERE id=?", (cl_dict["encounter_id"],)).fetchone()
        if not enc:
            continue
        billed_sum = c.execute(
            "SELECT COALESCE(SUM(total_price),0) s FROM services WHERE encounter_id=? AND billed=1",
            (enc["id"],),
        ).fetchone()["s"]
        total_charges = float(cl_dict["total_charges"] or 0)
        if billed_sum > 0 and total_charges < billed_sum - 0.01:
            diff = round(billed_sum - total_charges, 2)
            items.append({
                "category": "missing_charge",
                "category_label": CATEGORY_MAP["missing_charge"],
                "estimated_exposure": diff,
                "entity_type": "claim",
                "entity_id": cl_dict["id"],
                "claim_id": cl_dict["id"],
                "encounter_id": cl_dict["encounter_id"],
                "patient_id": cl_dict["patient_id"],
                "detection_source": "system_scan",
                "detection_rule": "missing_charge: claim.total_charges < sum(billed services)",
                "supporting_evidence": f"Claim {cl_dict['claim_number']} total {total_charges} is less than billed services {billed_sum}. Difference: GHS {diff:.2f}.",
                "recommended_action": "Review and amend the claim to include all billable services.",
                "priority": _priority_for("missing_charge"),
                "severity": _severity_for("missing_charge"),
            })
    return items


def _detect_duplicate_charges(c) -> List[Dict[str, Any]]:
    """C & F. Duplicate charges / duplicate billing risk: same service name multiple times on an encounter."""
    items = []
    encounters = c.execute("SELECT * FROM encounters").fetchall()
    for enc in encounters:
        enc_dict = dict(enc)
        services = c.execute(
            "SELECT id, service_name, total_price FROM services WHERE encounter_id=?", (enc_dict["id"],)
        ).fetchall()
        seen: Dict[str, List[Dict[str, Any]]] = {}
        for svc in services:
            seen.setdefault(svc["service_name"], []).append(dict(svc))
        for name, group in seen.items():
            if len(group) > 1:
                dup_amt = round(sum(float(s["total_price"] or 0) for s in group[1:]), 2)
                items.append({
                    "category": "duplicate_charge",
                    "category_label": CATEGORY_MAP["duplicate_charge"],
                    "estimated_exposure": dup_amt,
                    "entity_type": "service",
                    "entity_id": group[0]["id"],
                    "encounter_id": enc_dict["encounter_id"],
                    "patient_id": enc_dict["patient_id"],
                    "detection_source": "system_scan",
                    "detection_rule": "duplicate_charge: same service_name appears >1 time on encounter",
                    "supporting_evidence": f"Service '{name}' appears {len(group)} times on encounter {enc_dict['encounter_id']}. Duplicate amount: GHS {dup_amt:.2f}.",
                    "recommended_action": "Remove duplicate charge entries or confirm distinct services.",
                    "priority": _priority_for("duplicate_charge"),
                    "severity": _severity_for("duplicate_charge"),
                })
    return items


def _detect_rejected_claims(c) -> List[Dict[str, Any]]:
    """D. Unresolved rejected/denied claims."""
    items = []
    denied = c.execute(
        "SELECT * FROM claims WHERE status IN ('denied','rejected') AND (paid_amount IS NULL OR paid_amount=0)"
    ).fetchall()
    for cl in denied:
        cl_dict = dict(cl)
        amt = round(float(cl_dict["total_charges"] or 0), 2)
        items.append({
            "category": "rejected_claim_unresolved",
            "category_label": CATEGORY_MAP["rejected_claim_unresolved"],
            "estimated_exposure": amt,
            "entity_type": "claim",
            "entity_id": cl_dict["id"],
            "claim_id": cl_dict["id"],
            "encounter_id": cl_dict["encounter_id"],
            "patient_id": cl_dict["patient_id"],
            "detection_source": "system_scan",
            "detection_rule": "rejected_claim_unresolved: claim status denied/rejected with no payment",
            "supporting_evidence": f"Claim {cl_dict['claim_number']} is {cl_dict['status']} with no payment recorded. Reason: {cl_dict['denial_reason'] or 'not documented'}. Amount: GHS {amt:.2f}.",
            "recommended_action": "Work the denial: correct and resubmit or appeal.",
            "priority": _priority_for("rejected_claim_unresolved"),
            "severity": _severity_for("rejected_claim_unresolved"),
        })
    return items


def _detect_claim_not_followed_up(c) -> List[Dict[str, Any]]:
    """E. Claims submitted but not followed up (no payment, older)."""
    items = []
    pending_claims = c.execute(
        "SELECT * FROM claims WHERE status='submitted' AND (paid_amount IS NULL OR paid_amount=0)"
    ).fetchall()
    for cl in pending_claims:
        cl_dict = dict(cl)
        amt = round(float(cl_dict["total_charges"] or 0), 2)
        items.append({
            "category": "claim_not_followed_up",
            "category_label": CATEGORY_MAP["claim_not_followed_up"],
            "estimated_exposure": amt,
            "entity_type": "claim",
            "entity_id": cl_dict["id"],
            "claim_id": cl_dict["id"],
            "encounter_id": cl_dict["encounter_id"],
            "patient_id": cl_dict["patient_id"],
            "detection_source": "system_scan",
            "detection_rule": "claim_not_followed_up: claim submitted with no payment recorded",
            "supporting_evidence": f"Claim {cl_dict['claim_number']} submitted but no payment recorded and no follow-up action tracked. Amount: GHS {amt:.2f}.",
            "recommended_action": "Follow up with payer on claim status.",
            "priority": _priority_for("claim_not_followed_up"),
            "severity": _severity_for("claim_not_followed_up"),
        })
    return items


def _detect_unreconciled_payments(c) -> List[Dict[str, Any]]:
    """G. Unreconciled payments: bills with outstanding balance."""
    items = []
    bills = c.execute("SELECT * FROM bills WHERE outstanding > 0.01 AND status NOT IN ('paid','rejected')").fetchall()
    for bl in bills:
        bl_dict = dict(bl)
        amt = round(float(bl_dict["outstanding"] or 0), 2)
        items.append({
            "category": "unreconciled_payment",
            "category_label": CATEGORY_MAP["unreconciled_payment"],
            "estimated_exposure": amt,
            "entity_type": "bill",
            "entity_id": bl_dict["id"],
            "encounter_id": bl_dict["encounter_id"],
            "patient_id": bl_dict["patient_id"],
            "detection_source": "system_scan",
            "detection_rule": "unreconciled_payment: bill outstanding > 0 and not paid/rejected",
            "supporting_evidence": f"Bill {bl_dict['bill_number']} has outstanding balance {amt} and is not paid/rejected.",
            "recommended_action": "Reconcile payment or write off per policy.",
            "priority": _priority_for("unreconciled_payment"),
            "severity": _severity_for("unreconciled_payment"),
        })
    return items


def _detect_billed_vs_reimbursed(c) -> List[Dict[str, Any]]:
    """H. Billed vs reimbursed difference (underpayment)."""
    items = []
    paid_claims = c.execute("SELECT * FROM claims WHERE status='paid' AND (approved_amount IS NOT NULL AND approved_amount>0)").fetchall()
    for cl in paid_claims:
        cl_dict = dict(cl)
        billed = float(cl_dict["total_charges"] or 0)
        paid = float(cl_dict["paid_amount"] or 0)
        approved = float(cl_dict["approved_amount"] or 0)
        if billed > 0 and paid < approved - 0.01:
            diff = round(approved - paid, 2)
            items.append({
                "category": "billed_vs_reimbursed",
                "category_label": CATEGORY_MAP["billed_vs_reimbursed"],
                "estimated_exposure": diff,
                "entity_type": "claim",
                "entity_id": cl_dict["id"],
                "claim_id": cl_dict["id"],
                "encounter_id": cl_dict["encounter_id"],
                "patient_id": cl_dict["patient_id"],
                "detection_source": "system_scan",
                "detection_rule": "billed_vs_reimbursed: claim paid < approved amount",
                "supporting_evidence": f"Claim {cl_dict['claim_number']} approved {approved} but paid {paid}; underpayment of {diff}.",
                "recommended_action": "Pursue underpayment with payer or adjust balances.",
                "priority": _priority_for("billed_vs_reimbursed"),
                "severity": _severity_for("billed_vs_reimbursed"),
            })
    return items


def _detect_documentation_gaps(c) -> List[Dict[str, Any]]:
    """Documentation gaps: billing/claim records with missing supporting documentation."""
    items = []
    # Encounters with services but documentation incomplete
    encs = c.execute(
        """SELECT e.* FROM encounters e
           LEFT JOIN services s ON s.encounter_id = e.id
           WHERE e.documentation_complete = 0 AND s.id IS NOT NULL
           GROUP BY e.id"""
    ).fetchall()
    for e in encs:
        ed = dict(e)
        items.append({
            "category": "documentation_gap",
            "category_label": CATEGORY_MAP["documentation_gap"],
            "estimated_exposure": 0.0,
            "entity_type": "encounter",
            "entity_id": ed["id"],
            "encounter_id": ed["encounter_id"],
            "patient_id": ed.get("patient_id"),
            "detection_source": "system_scan",
            "detection_rule": "documentation_gap: encounter has services but documentation_complete=0",
            "supporting_evidence": f"Encounter {ed['encounter_id']} has recorded services but documentation is incomplete.",
            "recommended_action": "Complete clinical documentation to support billing.",
            "priority": _priority_for("documentation_gap"),
            "severity": _severity_for("documentation_gap"),
        })
    return items


def _detect_claim_preparation_errors(c) -> List[Dict[str, Any]]:
    """Claim preparation errors: claims with missing/inconsistent data before submission."""
    items = []
    claims = c.execute("SELECT * FROM claims WHERE status IN ('draft','vetted')").fetchall()
    for cl in claims:
        cl_dict = dict(cl)
        issues = []
        if not cl_dict.get("diagnosis_codes"):
            issues.append("missing diagnosis codes")
        if not cl_dict.get("procedure_codes"):
            issues.append("missing procedure codes")
        if not cl_dict.get("payer_name"):
            issues.append("missing payer name")
        if not cl_dict.get("claim_type"):
            issues.append("missing claim type")
        if issues:
            items.append({
                "category": "claim_preparation_errors",
                "category_label": CATEGORY_MAP["claim_preparation_errors"],
                "estimated_exposure": round(float(cl_dict["total_charges"] or 0), 2),
                "entity_type": "claim",
                "entity_id": cl_dict["id"],
                "claim_id": cl_dict["id"],
                "encounter_id": cl_dict["encounter_id"],
                "patient_id": cl_dict["patient_id"],
                "detection_source": "system_scan",
                "detection_rule": "claim_preparation_errors: claim missing required fields before submission",
                "supporting_evidence": f"Claim {cl_dict['claim_number']} has incomplete data: {', '.join(issues)}.",
                "recommended_action": "Correct claim data before submission.",
                "priority": _priority_for("claim_preparation_errors"),
                "severity": _severity_for("claim_preparation_errors"),
            })
    return items


def _detect_outstanding_revenue(c) -> List[Dict[str, Any]]:
    """Outstanding revenue: overdue receivables / outstanding claims."""
    items = []
    # Bills with outstanding balance not yet in leakage as unreconciled_payment (avoid dup)
    bills = c.execute("SELECT * FROM bills WHERE outstanding > 0.01 AND status='submitted'").fetchall()
    for bl in bills:
        bl_dict = dict(bl)
        amt = round(float(bl_dict["outstanding"] or 0), 2)
        items.append({
            "category": "outstanding_revenue",
            "category_label": CATEGORY_MAP["outstanding_revenue"],
            "estimated_exposure": amt,
            "entity_type": "bill",
            "entity_id": bl_dict["id"],
            "encounter_id": bl_dict["encounter_id"],
            "patient_id": bl_dict["patient_id"],
            "detection_source": "system_scan",
            "detection_rule": "outstanding_revenue: submitted bill with outstanding balance",
            "supporting_evidence": f"Bill {bl_dict['bill_number']} submitted with outstanding balance GHS {amt:.2f}.",
            "recommended_action": "Follow up on outstanding receivable.",
            "priority": _priority_for("outstanding_revenue"),
            "severity": _severity_for("outstanding_revenue"),
        })
    return items


def _detect_underbilling(c) -> List[Dict[str, Any]]:
    """Underbilling: bill total less than expected for services."""
    items = []
    bills = c.execute("SELECT * FROM bills WHERE status NOT IN ('paid','rejected')").fetchall()
    for bl in bills:
        bl_dict = dict(bl)
        line_sum = c.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM bill_line_items WHERE bill_id=?",
            (bl_dict["id"],),
        ).fetchone()["s"]
        total = float(bl_dict["total"] or 0)
        if line_sum > 0 and total < line_sum - 0.01:
            diff = round(line_sum - total, 2)
            items.append({
                "category": "underbilling",
                "category_label": CATEGORY_MAP["underbilling"],
                "estimated_exposure": diff,
                "entity_type": "bill",
                "entity_id": bl_dict["id"],
                "encounter_id": bl_dict["encounter_id"],
                "patient_id": bl_dict["patient_id"],
                "detection_source": "system_scan",
                "detection_rule": "underbilling: bill total < sum of line items",
                "supporting_evidence": f"Bill {bl_dict['bill_number']} total {total} is less than line items sum {line_sum}. Underbilled by GHS {diff:.2f}.",
                "recommended_action": "Review bill totals and correct underbilling.",
                "priority": _priority_for("underbilling"),
                "severity": _severity_for("underbilling"),
            })
    return items


DETECTION_RULES = {
    "unbilled_service": _detect_unbilled_services,
    "missing_charge": _detect_missing_charges,
    "duplicate_charge": _detect_duplicate_charges,
    "rejected_claim_unresolved": _detect_rejected_claims,
    "claim_not_followed_up": _detect_claim_not_followed_up,
    "unreconciled_payment": _detect_unreconciled_payments,
    "billed_vs_reimbursed": _detect_billed_vs_reimbursed,
    "documentation_gap": _detect_documentation_gaps,
    "claim_preparation_errors": _detect_claim_preparation_errors,
    "outstanding_revenue": _detect_outstanding_revenue,
    "underbilling": _detect_underbilling,
}


# ---------------------------------------------------------------------------
# Detection entry point
# ---------------------------------------------------------------------------

def detect_leakage(create_tasks: bool = True, categories: Optional[List[str]] = None) -> Dict[str, Any]:
    """Scan all encounters, claims, bills, and payments for leakage indicators.

    If create_tasks=True, persists findings to leakage_cases and creates
    orchestrator tasks routed to the Revenue Leakage Detection Agent.

    Only suspected indicators are created; nothing is auto-confirmed.
    """
    c = get_db()
    leakage_items: List[Dict[str, Any]] = []
    total_leakage = 0.0
    cases_created = 0
    tasks_created = 0

    rules = DETECTION_RULES
    if categories:
        rules = {k: v for k, v in DETECTION_RULES.items() if k in categories}

    for cat_name, rule_fn in rules.items():
        for item in rule_fn(c):
            total_leakage += item["estimated_exposure"]
            leakage_items.append(item)
            if create_tasks:
                cid, created = _persist_case(c, item)
                if created:
                    cases_created += 1
                    # Only create an orchestrator task for a NEW case. Repeat
                    # detection runs must not spawn duplicate tasks. If an open
                    # task already exists for this case, reuse it.
                    existing_task = c.execute(
                        "SELECT id FROM orchestrator_tasks WHERE entity_type='leakage_case' AND entity_id=? AND task_type='leakage_review' AND status IN ('created','assigned','started') LIMIT 1",
                        (cid,),
                    ).fetchone()
                    if not existing_task:
                        orch.create_orchestrator_task(
                            c, "leakage_review", "leakage_case", cid,
                            agent_name="Revenue Leakage Detection Agent",
                            direction="agent_to_human",
                            priority=item["priority"],
                            context=item["supporting_evidence"],
                            ai_recommendation=item["recommended_action"],
                            financial_impact=item["estimated_exposure"],
                            auto_assign=True,
                        )
                        tasks_created += 1

    leakage_items.sort(key=lambda x: x["estimated_exposure"], reverse=True)

    eid = orch.agent_execution_start(c, "Revenue Leakage Detection Agent", "detect_leakage", "system", None,
                                     {"create_tasks": create_tasks, "categories": categories}, None)
    orch.agent_execution_complete(c, eid, "completed",
                                  {"items": len(leakage_items), "total_leakage": round(total_leakage, 2),
                                   "cases_created": cases_created, "tasks_created": tasks_created},
                                  duration_ms=100)

    orch.set_agent_state("Revenue Leakage Detection Agent", "Completed", "Leakage scan",
                         {"items": len(leakage_items), "total_leakage": round(total_leakage, 2),
                          "cases_created": cases_created, "tasks_created": tasks_created})

    c.commit()
    c.close()
    return {
        "total_potential_leakage": round(total_leakage, 2),
        "item_count": len(leakage_items),
        "cases_created": cases_created,
        "tasks_created": tasks_created,
        "items": leakage_items,
        "notice": "Flagged items are suspected leakage indicators, not confirmed fraud. Human review is required before any action.",
    }


# ---------------------------------------------------------------------------
# Case management
# ---------------------------------------------------------------------------

def list_leakage_cases(filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """List leakage cases with optional filtering."""
    c = get_db()
    q = "SELECT * FROM leakage_cases WHERE 1=1"
    params: List[Any] = []
    f = filters or {}
    if f.get("category"):
        q += " AND category=?"; params.append(f["category"])
    if f.get("priority"):
        q += " AND priority=?"; params.append(f["priority"])
    if f.get("status"):
        q += " AND status=?"; params.append(f["status"])
    if f.get("assigned_user"):
        q += " AND assigned_user=?"; params.append(f["assigned_user"])
    if f.get("min_amount"):
        q += " AND COALESCE(estimated_exposure, amount, 0) >= ?"; params.append(float(f["min_amount"]))
    if f.get("max_amount"):
        q += " AND COALESCE(estimated_exposure, amount, 0) <= ?"; params.append(float(f["max_amount"]))
    if f.get("date_from"):
        q += " AND created_at >= ?"; params.append(f["date_from"])
    if f.get("date_to"):
        q += " AND created_at <= ?"; params.append(f["date_to"])
    q += " ORDER BY created_at DESC"
    rows = c.execute(q, params).fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        d["estimated_exposure"] = float(d["estimated_exposure"]) if d["estimated_exposure"] is not None else float(d.get("amount") or 0)
        out.append(d)
    return out


def get_leakage_case(case_id: str) -> Optional[Dict[str, Any]]:
    """Get a single leakage case by ID."""
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    c.close()
    if not row:
        return None
    d = dict(row)
    d["estimated_exposure"] = float(d["estimated_exposure"]) if d["estimated_exposure"] is not None else float(d.get("amount") or 0)
    return d


def assign_leakage_case(case_id: str, assigned_user: str, actor: str = "User") -> Dict[str, Any]:
    """Assign a leakage case to a responsible staff member.
    Also transitions the case to UNDER_REVIEW if it's currently DETECTED."""
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return {"error": "Leakage case not found"}, 404
    current = row["status"]
    c.execute(
        """UPDATE leakage_cases SET assigned_user=?, updated_at=? WHERE id=?""",
        (assigned_user, now(), case_id),
    )
    if current == "DETECTED":
        c.execute("UPDATE leakage_cases SET status='UNDER_REVIEW', updated_at=? WHERE id=?", (now(), case_id))
    audit(c, actor, "user", "Revenue Leakage Detection Agent",
          f"Leakage case assigned to {assigned_user}", "leakage_case", case_id,
          previous_state=json.dumps({"assigned_user": row["assigned_user"], "status": current}),
          new_state=json.dumps({"assigned_user": assigned_user, "status": "UNDER_REVIEW" if current == "DETECTED" else current}),
          input_summary=json.dumps({"assigned_user": assigned_user, "actor": actor}))
    c.commit(); c.close()
    return {"ok": True, "case_id": case_id, "assigned_user": assigned_user}


def prioritize_leakage_case(case_id: str, priority: str, actor: str = "User") -> Dict[str, Any]:
    """Change the priority of a leakage case."""
    if priority not in ("low", "medium", "high", "critical"):
        return {"error": "Invalid priority. Must be low, medium, high, or critical."}, 400
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return {"error": "Leakage case not found"}, 404
    c.execute(
        """UPDATE leakage_cases SET priority=?, updated_at=? WHERE id=?""",
        (priority, now(), case_id),
    )
    audit(c, actor, "user", "Revenue Leakage Detection Agent",
          f"Leakage case priority changed to {priority}", "leakage_case", case_id,
          previous_state=json.dumps({"priority": row["priority"]}),
          new_state=json.dumps({"priority": priority}),
          input_summary=json.dumps({"priority": priority, "actor": actor}))
    c.commit(); c.close()
    return {"ok": True, "case_id": case_id, "priority": priority}


def confirm_leakage_case(case_id: str, actor: str = "User") -> Dict[str, Any]:
    """Confirm a leakage case as a real issue (UNDER_REVIEW -> CONFIRMED)."""
    return _transition_case(case_id, "CONFIRMED", actor, reason="Case confirmed as real leakage issue")


def require_action(case_id: str, actor: str = "User") -> Dict[str, Any]:
    """Transition a confirmed case to ACTION_REQUIRED (CONFIRMED -> ACTION_REQUIRED)."""
    return _transition_case(case_id, "ACTION_REQUIRED", actor, reason="Corrective action required")


def dismiss_leakage_case(case_id: str, actor: str = "User") -> Dict[str, Any]:
    """Dismiss a leakage case without action."""
    return _transition_case(case_id, "DISMISSED", actor, reason="Case dismissed")


def mark_false_positive(case_id: str, reason: str, actor: str = "User") -> Dict[str, Any]:
    """Mark a leakage case as a false positive with a reason."""
    if not reason:
        return {"error": "false_positive_reason is required"}, 400
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return {"error": "Leakage case not found"}, 404
    current = row["status"]
    if current not in VALID_TRANSITIONS or "FALSE_POSITIVE" not in VALID_TRANSITIONS[current]:
        c.close()
        return {"error": f"Cannot mark '{current}' as false positive"}, 400
    c.execute(
        """UPDATE leakage_cases SET status='FALSE_POSITIVE', false_positive_reason=?,
            updated_at=?, resolved_at=? WHERE id=?""",
        (reason, now(), now(), case_id),
    )
    audit(c, actor, "user", "Revenue Leakage Detection Agent",
          "Leakage case marked false positive", "leakage_case", case_id,
          previous_state=json.dumps({"status": current}),
          new_state=json.dumps({"status": "FALSE_POSITIVE", "reason": reason}),
          input_summary=json.dumps({"reason": reason, "actor": actor}))
    c.commit(); c.close()
    return {"ok": True, "case_id": case_id, "status": "FALSE_POSITIVE", "false_positive_reason": reason}


def reopen_leakage_case(case_id: str, actor: str = "User") -> Dict[str, Any]:
    """Reopen a resolved case."""
    return _transition_case(case_id, "REOPENED", actor, reason="Case reopened")


def start_corrective_action(case_id: str, actor: str = "User") -> Dict[str, Any]:
    """Start corrective action on a confirmed case (ACTION_REQUIRED -> IN_PROGRESS)."""
    return _transition_case(case_id, "IN_PROGRESS", actor, reason="Corrective action started")


def resolve_leakage_case(case_id: str, actor: str = "User",
                         resolution_notes: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a leakage case (IN_PROGRESS -> RESOLVED)."""
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return {"error": "Leakage case not found"}, 404
    if row["status"] != "IN_PROGRESS":
        c.close()
        return {"error": f"Case must be in IN_PROGRESS status to resolve. Current: {row['status']}"}, 400
    c.execute(
        """UPDATE leakage_cases SET status='RESOLVED', updated_at=?, resolved_at=?,
            resolution_notes=COALESCE(?, resolution_notes) WHERE id=?""",
        (now(), now(), resolution_notes, case_id),
    )
    audit(c, actor, "user", "Revenue Leakage Detection Agent",
          "Leakage case resolved", "leakage_case", case_id,
          previous_state=json.dumps({"status": row["status"]}),
          new_state=json.dumps({"status": "RESOLVED", "notes": resolution_notes or ""}),
          input_summary=json.dumps({"resolution_notes": resolution_notes or "", "actor": actor}))
    c.commit(); c.close()
    return {"ok": True, "case_id": case_id, "status": "RESOLVED"}


def add_case_notes(case_id: str, notes: str, actor: str = "User") -> Dict[str, Any]:
    """Add notes to a leakage case."""
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return {"error": "Leakage case not found"}, 404
    existing = row["resolution_notes"] or ""
    combined = f"{existing}\n[{now()}] {actor}: {notes}" if existing else f"[{now()}] {actor}: {notes}"
    c.execute(
        """UPDATE leakage_cases SET resolution_notes=?, updated_at=? WHERE id=?""",
        (combined, now(), case_id),
    )
    audit(c, actor, "user", "Revenue Leakage Detection Agent",
          "Leakage case note added", "leakage_case", case_id,
          input_summary=json.dumps({"notes": notes, "actor": actor}))
    c.commit(); c.close()
    return {"ok": True, "case_id": case_id, "notes": combined}


def record_confirmed_amount(case_id: str, amount: float, actor: str = "User") -> Dict[str, Any]:
    """Record the confirmed leakage amount for a case."""
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return {"error": "Leakage case not found"}, 404
    c.execute(
        """UPDATE leakage_cases SET confirmed_amount=?, updated_at=? WHERE id=?""",
        (round(float(amount), 2), now(), case_id),
    )
    audit(c, actor, "user", "Revenue Leakage Detection Agent",
          f"Confirmed amount recorded: GHS {amount:.2f}", "leakage_case", case_id,
          previous_state=json.dumps({"confirmed_amount": row["confirmed_amount"]}),
          new_state=json.dumps({"confirmed_amount": round(float(amount), 2)}),
          input_summary=json.dumps({"confirmed_amount": amount, "actor": actor}))
    c.commit(); c.close()
    return {"ok": True, "case_id": case_id, "confirmed_amount": round(float(amount), 2)}


def record_recovered_amount(case_id: str, amount: float, actor: str = "User") -> Dict[str, Any]:
    """Record verified recovered amount for a case."""
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return {"error": "Leakage case not found"}, 404
    current = float(row["verified_recovered_amount"] or 0)
    new_total = round(current + float(amount), 2)
    c.execute(
        """UPDATE leakage_cases SET verified_recovered_amount=?, updated_at=? WHERE id=?""",
        (new_total, now(), case_id),
    )
    audit(c, actor, "user", "Revenue Leakage Detection Agent",
          f"Recovered amount recorded: GHS {amount:.2f} (total: GHS {new_total:.2f})", "leakage_case", case_id,
          previous_state=json.dumps({"verified_recovered_amount": current}),
          new_state=json.dumps({"verified_recovered_amount": new_total}),
          input_summary=json.dumps({"recovered_amount": amount, "total_recovered": new_total, "actor": actor}))
    c.commit(); c.close()
    return {"ok": True, "case_id": case_id, "verified_recovered_amount": new_total}


def leakage_analytics() -> Dict[str, Any]:
    """Analytics for leakage cases derived from actual data."""
    c = get_db()
    cases = [dict(r) for r in c.execute("SELECT * FROM leakage_cases").fetchall()]

    by_category: Dict[str, Dict[str, Any]] = {}
    by_status: Dict[str, int] = {}
    by_priority: Dict[str, int] = {}
    estimated_exposure = 0.0
    confirmed_amount = 0.0
    recovered_amount = 0.0
    outstanding_amount = 0.0

    for case in cases:
        cat = case["category"] or "uncategorized"
        est = float(case["estimated_exposure"] if case["estimated_exposure"] is not None else (case["amount"] or 0))
        if cat not in by_category:
            by_category[cat] = {"count": 0, "estimated_exposure": 0.0, "confirmed_amount": 0.0, "recovered_amount": 0.0}
        by_category[cat]["count"] += 1
        by_category[cat]["estimated_exposure"] += est
        by_category[cat]["confirmed_amount"] += float(case["confirmed_amount"] or 0)
        by_category[cat]["recovered_amount"] += float(case["verified_recovered_amount"] or 0)

        st = case["status"] or "DETECTED"
        by_status[st] = by_status.get(st, 0) + 1

        pr = case["priority"] or "medium"
        by_priority[pr] = by_priority.get(pr, 0) + 1

        estimated_exposure += est
        confirmed_amount += float(case["confirmed_amount"] or 0)
        recovered_amount += float(case["verified_recovered_amount"] or 0)
        if case["status"] not in TERMINAL_STATES:
            outstanding_amount += est - float(case["verified_recovered_amount"] or 0)

    c.close()
    return {
        "currency": "GHS",
        "total_cases": len(cases),
        "estimated_exposure": round(estimated_exposure, 2),
        "confirmed_amount": round(confirmed_amount, 2),
        "verified_recovered_amount": round(recovered_amount, 2),
        "outstanding_amount": round(max(0, outstanding_amount), 2),
        "by_category": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by_category.items()},
        "by_status": by_status,
        "by_priority": by_priority,
        "distinctions": {
            "estimated_vs_confirmed": "Estimated exposure is potential, not confirmed recoverable revenue",
            "confirmed_vs_recovered": "Confirmed amount is not recovered until payment is verified",
        },
    }
