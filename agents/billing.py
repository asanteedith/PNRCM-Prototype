"""Billing Agent.

Generates bills from encounters and recorded services, validates billing
calculations, detects duplicate charges, identifies missing charges, flags
incorrect totals, and maintains billing status history.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Tuple

from db import get_db, now, agent_execution_start, agent_execution_complete, audit

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _service_duplicates(services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    seen: Dict[str, List[Dict[str, Any]]] = {}
    for svc in services:
        key = svc["service_name"]
        seen.setdefault(key, []).append(svc)
    for key, group in seen.items():
        if len(group) > 1:
            issues.append({
                "code": "DUPLICATE_CHARGE",
                "severity": "high",
                "issue": f"Duplicate charge for service '{key}'.",
                "detail": f"{len(group)} entries for '{key}' on the same encounter.",
                "recommended_correction": "Remove duplicate entries or verify they represent distinct services.",
                "explanation": "The same service appears more than once on the encounter, which may indicate a duplicate billing error.",
                "service_ids": [s["id"] for s in group],
            })
    return issues


def _missing_charges(services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    unbilled = [s for s in services if not s["billed"]]
    for svc in unbilled:
        issues.append({
            "code": "MISSING_CHARGE",
            "severity": "medium",
            "issue": f"Service '{svc['service_name']}' delivered but not billed.",
            "detail": f"Service id {svc['id']} has billed=0.",
            "recommended_correction": "Add the service to the bill or mark it as billed.",
            "explanation": "Delivered services must appear on the bill to capture revenue.",
            "service_id": svc["id"],
            "amount": svc["total_price"],
        })
    return issues


def _validate_totals(bill: Dict[str, Any], services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    expected = sum(float(s["total_price"]) for s in services if s["billed"])
    subtotal = float(bill["subtotal"] or 0)
    tax = float(bill["tax"] or 0)
    discount = float(bill["discount"] or 0)
    total = float(bill["total"] or 0)
    calculated = subtotal + tax - discount
    if abs(total - calculated) > 0.01:
        issues.append({
            "code": "INCORRECT_TOTAL",
            "severity": "medium",
            "issue": "Bill total does not match subtotal + tax - discount.",
            "detail": f"Bill total {total} vs calculated {calculated}.",
            "recommended_correction": "Recalculate the bill total from its components.",
            "explanation": "The stored total is arithmetically inconsistent with its components.",
        })
    if abs(subtotal - expected) > 0.01 and services:
        issues.append({
            "code": "SUBTOTAL_MISMATCH",
            "severity": "medium",
            "issue": "Bill subtotal does not match summed billed service charges.",
            "detail": f"Subtotal {subtotal} vs billed services {expected}.",
            "recommended_correction": "Reconcile subtotal with billed service charges.",
            "explanation": "The subtotal should equal the sum of billed services.",
        })
    return issues


def generate_bill(encounter_id: str) -> Dict[str, Any]:
    """Generate (or regenerate) a draft bill from an encounter's billed services.

    Does not auto-submit or auto-charge; returns a draft bill for human review.
    """
    c = get_db()
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (encounter_id, encounter_id)).fetchone()
    if not enc:
        c.close()
        return {"error": "Encounter not found"}, 404
    enc_dict = dict(enc)
    patient_id = enc_dict["patient_id"]
    services = c.execute(
        "SELECT * FROM services WHERE encounter_id=? AND billed=1 ORDER BY created_at",
        (enc_dict["id"],),
    ).fetchall()
    service_list = [dict(s) for s in services]
    duplicates = _service_duplicates(service_list)
    missing = _missing_charges(service_list)

    subtotal = sum(float(s["total_price"]) for s in service_list)
    tax = 0.0
    discount = 0.0
    total = subtotal + tax - discount

    # Check for an existing draft bill
    existing = c.execute(
        "SELECT * FROM bills WHERE encounter_id=? AND status='draft' ORDER BY created_at DESC LIMIT 1",
        (enc_dict["id"],),
    ).fetchone()

    bid = str(uuid.uuid4())
    bill_number = "BILL-" + str(uuid.uuid4())[:8].upper()
    if existing:
        bid = existing["id"]
        bill_number = existing["bill_number"]
        c.execute(
            """UPDATE bills SET subtotal=?, tax=?, discount=?, total=?, outstanding=?, updated_at=? WHERE id=?""",
            (subtotal, tax, discount, total, total, now(), bid),
        )
        previous = existing["status"]
        new_state = "draft"
    else:
        c.execute(
            """INSERT INTO bills(id, bill_number, encounter_id, patient_id, subtotal, tax, discount, total, paid, outstanding, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,0,?, 'draft', ?, ?)""",
            (bid, bill_number, enc_dict["id"], patient_id, subtotal, tax, discount, total, total, now(), now()),
        )
        previous = None
        new_state = "draft"

    issues = duplicates + missing
    issues.extend(_validate_totals({"subtotal": subtotal, "tax": tax, "discount": discount, "total": total}, service_list))

    status = "pending_review" if issues else "draft"
    c.execute("UPDATE bills SET status=?, updated_at=? WHERE id=?", (status, now(), bid))
    audit(
        c,
        "Billing Agent",
        "agent",
        "Billing",
        "Bill generated from encounter services",
        "bill",
        bid,
        previous_state=previous,
        new_state=status,
        recommendation=status,
        decision=None,
    )
    c.commit()
    row = c.execute("SELECT * FROM bills WHERE id=?", (bid,)).fetchone()
    c.close()
    return {
        "bill_id": row["id"],
        "bill_number": row["bill_number"],
        "encounter_id": enc_dict["id"],
        "patient_id": patient_id,
        "subtotal": row["subtotal"],
        "tax": row["tax"],
        "discount": row["discount"],
        "total": row["total"],
        "status": row["status"],
        "issues": issues,
        "issue_count": len(issues),
        "billed_service_count": len(service_list),
        "unbilled_service_count": len(missing),
    }


def validate_bill(bill_id: str) -> Dict[str, Any]:
    """Validate an existing bill's calculations and charges."""
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bill_id, bill_id)).fetchone()
    if not bill:
        c.close()
        return {"error": "Bill not found"}, 404
    bill_dict = dict(bill)
    services = c.execute(
        "SELECT * FROM services WHERE encounter_id=? AND billed=1 ORDER BY created_at",
        (bill_dict["encounter_id"],),
    ).fetchall()
    service_list = [dict(s) for s in services]
    issues = _service_duplicates(service_list)
    issues.extend(_missing_charges(service_list))
    issues.extend(_validate_totals(bill_dict, service_list))
    severity = "low"
    for it in issues:
        if SEVERITY_ORDER.get(it["severity"], 0) > SEVERITY_ORDER.get(severity, 0):
            severity = it["severity"]
    overall = "PASS"
    if any(i["severity"] == "critical" for i in issues):
        overall = "BLOCKED"
    elif any(i["severity"] == "high" for i in issues):
        overall = "NEEDS_CORRECTION"
    elif issues:
        overall = "PASS_WITH_WARNINGS"
    c.execute("UPDATE bills SET updated_at=? WHERE id=?", (now(), bill_dict["id"]))
    audit(
        c,
        "Billing Agent",
        "agent",
        "Billing Validation",
        "Bill validated",
        "bill",
        bill_dict["id"],
        previous_state=bill_dict["status"],
        new_state=bill_dict["status"],
        recommendation=overall,
        decision=None,
    )
    c.commit()
    c.close()
    return {
        "bill_id": bill_dict["id"],
        "bill_number": bill_dict["bill_number"],
        "validation_status": overall,
        "severity": severity,
        "issues": issues,
        "issue_count": len(issues),
    }
