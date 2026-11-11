"""Revenue Leakage Detection Agent.

Identifies possible revenue leakage such as unbilled services, missing charges,
duplicate/inconsistent charges, underbilling, unresolved rejected claims,
claims not followed up, unreconciled payments, and differences between billed
and reimbursed amounts.

Suspected leakage is NOT labeled as confirmed fraud.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

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
}


def _severity_for(category: str) -> str:
    return SEVERITY_MAP.get(category, "medium")


def _existing_case(c, entity_type: str, entity_id: str, category: str) -> Dict[str, Any] | None:
    """Check if a leakage case already exists for this entity/category."""
    row = c.execute(
        "SELECT * FROM leakage_cases WHERE entity_type=? AND entity_id=? AND category=? AND status IN ('Detected','In Review')",
        (entity_type, entity_id, category),
    ).fetchone()
    return dict(row) if row else None


def _persist_case(c, item: Dict[str, Any]) -> str:
    """Insert or update a leakage case in leakage_cases."""
    existing = _existing_case(c, item["entity_type"], item["entity_id"], item["category"])
    if existing:
        c.execute(
            """UPDATE leakage_cases SET amount=?, evidence=?, recommended_action=?, updated_at=? WHERE id=?""",
            (item["amount"], item["evidence"], item["recommended_action"], now(), existing["id"]),
        )
        return existing["id"]
    cid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO leakage_cases(id,category,amount,estimated,entity_type,entity_id,patient_id,encounter_id,claim_id,evidence,recommended_action,status,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'Detected',?,?)""",
        (
            cid,
            item["category"],
            item["amount"],
            1,
            item["entity_type"],
            item["entity_id"],
            item.get("patient_id"),
            item.get("encounter_id"),
            item.get("claim_id"),
            item["evidence"],
            item["recommended_action"],
            now(),
            now(),
        ),
    )
    return cid


def detect_leakage(create_tasks: bool = True) -> Dict[str, Any]:
    """Scan all encounters, claims, bills, and payments for leakage indicators.

    If create_tasks=True, persists findings to leakage_cases and creates
    orchestrator tasks routed to the Revenue Leakage Detection Agent.
    """
    c = get_db()
    leakage_items: List[Dict[str, Any]] = []
    total_leakage = 0.0
    cases_created = 0
    tasks_created = 0

    # 1. Unbilled services (services provided but not billed)
    unbilled = c.execute(
        """SELECT s.id, s.service_name, s.total_price, s.encounter_id, e.encounter_id as enc_no, e.patient_id
        FROM services s JOIN encounters e ON s.encounter_id=e.id
        WHERE s.billed=0"""
    ).fetchall()
    for row in unbilled:
        amt = float(row["total_price"])
        total_leakage += amt
        item = {
            "id": str(uuid.uuid4()),
            "category": "unbilled_service",
            "category_label": CATEGORY_MAP["unbilled_service"],
            "amount": amt,
            "entity_type": "service",
            "entity_id": row["id"],
            "encounter_id": row["enc_no"],
            "patient_id": row["patient_id"],
            "evidence": f"Service '{row['service_name']}' delivered but not billed (billed=0).",
            "recommended_action": "Add the service to a bill or mark as non-billable.",
            "status": "open",
            "confirmed_fraud": False,
            "severity": _severity_for("unbilled_service"),
        }
        leakage_items.append(item)
        if create_tasks:
            cid = _persist_case(c, item)
            cases_created += 1
            orch.create_orchestrator_task(
                c, "leakage_review", "service", row["id"],
                agent_name="Revenue Leakage Detection Agent",
                direction="agent_to_human",
                priority=item["severity"],
                context=item["evidence"],
                ai_recommendation=item["recommended_action"],
                financial_impact=amt,
                auto_assign=True,
            )
            tasks_created += 1

    # 2. Missing charges on claims (claim total < sum of billed services)
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
            diff = billed_sum - total_charges
            total_leakage += diff
            item = {
                "id": str(uuid.uuid4()),
                "category": "missing_charge",
                "category_label": CATEGORY_MAP["missing_charge"],
                "amount": round(diff, 2),
                "entity_type": "claim",
                "entity_id": cl_dict["id"],
                "claim_id": cl_dict["id"],
                "encounter_id": cl_dict["encounter_id"],
                "patient_id": cl_dict["patient_id"],
                "evidence": f"Claim {cl_dict['claim_number']} total {total_charges} is less than billed services {billed_sum}.",
                "recommended_action": "Review and amend the claim to include all billable services.",
                "status": "open",
                "confirmed_fraud": False,
                "severity": _severity_for("missing_charge"),
            }
            leakage_items.append(item)
            if create_tasks:
                cid = _persist_case(c, item)
                cases_created += 1
                orch.create_orchestrator_task(
                    c, "leakage_review", "claim", cl_dict["id"],
                    agent_name="Revenue Leakage Detection Agent",
                    direction="agent_to_human",
                    priority=item["severity"],
                    context=item["evidence"],
                    ai_recommendation=item["recommended_action"],
                    financial_impact=round(diff, 2),
                    auto_assign=True,
                )
                tasks_created += 1

    # 3. Duplicate charges (same service name multiple times on an encounter)
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
                dup_amt = sum(float(s["total_price"]) for s in group[1:])
                total_leakage += dup_amt
                item = {
                    "id": str(uuid.uuid4()),
                    "category": "duplicate_charge",
                    "category_label": CATEGORY_MAP["duplicate_charge"],
                    "amount": round(dup_amt, 2),
                    "entity_type": "service",
                    "entity_id": group[0]["id"],
                    "encounter_id": enc_dict["encounter_id"],
                    "patient_id": enc_dict["patient_id"],
                    "evidence": f"Service '{name}' appears {len(group)} times on encounter {enc_dict['encounter_id']}.",
                    "recommended_action": "Remove duplicate charge entries or confirm distinct services.",
                    "status": "open",
                    "confirmed_fraud": False,
                    "severity": _severity_for("duplicate_charge"),
                }
                leakage_items.append(item)
                if create_tasks:
                    cid = _persist_case(c, item)
                    cases_created += 1
                    orch.create_orchestrator_task(
                        c, "leakage_review", "service", group[0]["id"],
                        agent_name="Revenue Leakage Detection Agent",
                        direction="agent_to_human",
                        priority=item["severity"],
                        context=item["evidence"],
                        ai_recommendation=item["recommended_action"],
                        financial_impact=round(dup_amt, 2),
                        auto_assign=True,
                    )
                    tasks_created += 1

    # 4. Unresolved rejected/denied claims
    denied = c.execute(
        "SELECT * FROM claims WHERE status IN ('denied','rejected') AND (paid_amount IS NULL OR paid_amount=0)"
    ).fetchall()
    for cl in denied:
        cl_dict = dict(cl)
        amt = float(cl_dict["total_charges"] or 0)
        total_leakage += amt
        item = {
            "id": str(uuid.uuid4()),
            "category": "rejected_claim_unresolved",
            "category_label": CATEGORY_MAP["rejected_claim_unresolved"],
            "amount": round(amt, 2),
            "entity_type": "claim",
            "entity_id": cl_dict["id"],
            "claim_id": cl_dict["id"],
            "encounter_id": cl_dict["encounter_id"],
            "patient_id": cl_dict["patient_id"],
            "evidence": f"Claim {cl_dict['claim_number']} is {cl_dict['status']} with no payment recorded. Reason: {cl_dict['denial_reason'] or 'not documented'}.",
            "recommended_action": "Work the denial: correct and resubmit or appeal.",
            "status": "open",
            "confirmed_fraud": False,
            "severity": _severity_for("rejected_claim_unresolved"),
        }
        leakage_items.append(item)
        if create_tasks:
            cid = _persist_case(c, item)
            cases_created += 1
            orch.create_orchestrator_task(
                c, "leakage_review", "claim", cl_dict["id"],
                agent_name="Revenue Leakage Detection Agent",
                direction="agent_to_human",
                priority=item["severity"],
                context=item["evidence"],
                ai_recommendation=item["recommended_action"],
                financial_impact=amt,
                auto_assign=True,
            )
            tasks_created += 1

    # 5. Claims submitted but not followed up (no payment, older)
    pending_claims = c.execute(
        "SELECT * FROM claims WHERE status='submitted' AND (paid_amount IS NULL OR paid_amount=0)"
    ).fetchall()
    for cl in pending_claims:
        cl_dict = dict(cl)
        amt = float(cl_dict["total_charges"] or 0)
        total_leakage += amt
        item = {
            "id": str(uuid.uuid4()),
            "category": "claim_not_followed_up",
            "category_label": CATEGORY_MAP["claim_not_followed_up"],
            "amount": round(amt, 2),
            "entity_type": "claim",
            "entity_id": cl_dict["id"],
            "claim_id": cl_dict["id"],
            "encounter_id": cl_dict["encounter_id"],
            "patient_id": cl_dict["patient_id"],
            "evidence": f"Claim {cl_dict['claim_number']} submitted but no payment recorded and no follow-up action tracked.",
            "recommended_action": "Follow up with payer on claim status.",
            "status": "open",
            "confirmed_fraud": False,
            "severity": _severity_for("claim_not_followed_up"),
        }
        leakage_items.append(item)
        if create_tasks:
            cid = _persist_case(c, item)
            cases_created += 1
            orch.create_orchestrator_task(
                c, "leakage_review", "claim", cl_dict["id"],
                agent_name="Revenue Leakage Detection Agent",
                direction="agent_to_human",
                priority=item["severity"],
                context=item["evidence"],
                ai_recommendation=item["recommended_action"],
                financial_impact=amt,
                auto_assign=True,
            )
            tasks_created += 1

    # 6. Unreconciled payments (bills with outstanding balance but payments incomplete)
    bills = c.execute("SELECT * FROM bills WHERE outstanding > 0.01 AND status NOT IN ('paid','rejected')").fetchall()
    for bl in bills:
        bl_dict = dict(bl)
        amt = float(bl_dict["outstanding"] or 0)
        total_leakage += amt
        item = {
            "id": str(uuid.uuid4()),
            "category": "unreconciled_payment",
            "category_label": CATEGORY_MAP["unreconciled_payment"],
            "amount": round(amt, 2),
            "entity_type": "bill",
            "entity_id": bl_dict["id"],
            "encounter_id": bl_dict["encounter_id"],
            "patient_id": bl_dict["patient_id"],
            "evidence": f"Bill {bl_dict['bill_number']} has outstanding balance {amt} and is not paid/rejected.",
            "recommended_action": "Reconcile payment or write off per policy.",
            "status": "open",
            "confirmed_fraud": False,
            "severity": _severity_for("unreconciled_payment"),
        }
        leakage_items.append(item)
        if create_tasks:
            cid = _persist_case(c, item)
            cases_created += 1
            orch.create_orchestrator_task(
                c, "leakage_review", "bill", bl_dict["id"],
                agent_name="Revenue Leakage Detection Agent",
                direction="agent_to_human",
                priority=item["severity"],
                context=item["evidence"],
                ai_recommendation=item["recommended_action"],
                financial_impact=amt,
                auto_assign=True,
            )
            tasks_created += 1

    # 7. Billed vs reimbursed difference (underpayment)
    paid_claims = c.execute("SELECT * FROM claims WHERE status='paid' AND (approved_amount IS NOT NULL AND approved_amount>0)").fetchall()
    for cl in paid_claims:
        cl_dict = dict(cl)
        billed = float(cl_dict["total_charges"] or 0)
        paid = float(cl_dict["paid_amount"] or 0)
        approved = float(cl_dict["approved_amount"] or 0)
        if billed > 0 and paid < approved - 0.01:
            diff = approved - paid
            total_leakage += diff
            item = {
                "id": str(uuid.uuid4()),
                "category": "billed_vs_reimbursed",
                "category_label": CATEGORY_MAP["billed_vs_reimbursed"],
                "amount": round(diff, 2),
                "entity_type": "claim",
                "entity_id": cl_dict["id"],
                "claim_id": cl_dict["id"],
                "encounter_id": cl_dict["encounter_id"],
                "patient_id": cl_dict["patient_id"],
                "evidence": f"Claim {cl_dict['claim_number']} approved {approved} but paid {paid}; underpayment of {diff}.",
                "recommended_action": "Pursue underpayment with payer or adjust balances.",
                "status": "open",
                "confirmed_fraud": False,
                "severity": _severity_for("billed_vs_reimbursed"),
            }
            leakage_items.append(item)
            if create_tasks:
                cid = _persist_case(c, item)
                cases_created += 1
                orch.create_orchestrator_task(
                    c, "leakage_review", "claim", cl_dict["id"],
                    agent_name="Revenue Leakage Detection Agent",
                    direction="agent_to_human",
                    priority=item["severity"],
                    context=item["evidence"],
                    ai_recommendation=item["recommended_action"],
                    financial_impact=round(diff, 2),
                    auto_assign=True,
                )
                tasks_created += 1

    # Sort by amount descending
    leakage_items.sort(key=lambda x: x["amount"], reverse=True)

    # Record agent execution
    eid = orch.agent_execution_start(c, "Revenue Leakage Detection Agent", "detect_leakage", "system", None,
                                     {"create_tasks": create_tasks}, None)
    orch.agent_execution_complete(c, eid, "completed",
                                  {"items": len(leakage_items), "total_leakage": round(total_leakage, 2),
                                   "cases_created": cases_created, "tasks_created": tasks_created},
                                  duration_ms=100)

    set_agent_state = orch.set_agent_state
    set_agent_state("Revenue Leakage Detection Agent", "Completed", "Leakage scan",
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


def resolve_leakage_case(case_id: str, resolution: str, resolved_by: str = "User") -> Dict[str, Any]:
    """Resolve a leakage case (Detected -> Resolved/False Positive)."""
    c = get_db()
    row = c.execute("SELECT * FROM leakage_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return {"error": "Leakage case not found"}, 404
    status = "Resolved" if resolution == "resolved" else "False Positive"
    c.execute(
        "UPDATE leakage_cases SET status=?, updated_at=?, resolved_at=? WHERE id=?",
        (status, now(), now(), case_id),
    )
    audit(c, resolved_by, "user", "Revenue Leakage Detection Agent", f"Leakage case {resolution}",
          "leakage_case", case_id, new_state=status)
    c.commit(); c.close()
    return {"ok": True, "case_id": case_id, "status": status}
