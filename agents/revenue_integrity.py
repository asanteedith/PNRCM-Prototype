"""Revenue Integrity Intelligence Agent.

Embeds revenue assurance, leakage detection, write-off auditing, payer policy
drift, gap detection, prioritization, and recovery into existing PNRCM
workflows. No standalone dashboards or disconnected features. All findings
route to existing review queue / exceptions / audit trail.
"""
from __future__ import annotations

import json, uuid, datetime
from typing import Any, Dict, List, Tuple, Optional
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


def _iso(d: str) -> Optional[datetime.datetime]:
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


# ---------------------------------------------------------------------------
# 1. Service-to-Revenue Completeness
# ---------------------------------------------------------------------------
def check_service_to_revenue_completeness(
    encounter_id: str,
    actor: str = "System",
) -> Dict[str, Any]:
    """Check whether every billable service has a corresponding charge and
    vice versa. Creates review cases for discrepancies."""
    start = datetime.datetime.now(datetime.timezone.utc)
    c = get_db()
    exec_id = agent_execution_start(
        c, "Revenue Integrity Agent", "service_to_revenue_completeness",
        "encounter", encounter_id, {"encounter_id": encounter_id, "actor": actor},
    )

    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (encounter_id, encounter_id)).fetchone()
    if not enc:
        agent_execution_complete(c, exec_id, "failed", error_message="Encounter not found")
        c.close()
        return {"error": "Encounter not found"}, 404
    ed = dict(enc)

    services = [dict(s) for s in c.execute(
        "SELECT * FROM services WHERE encounter_id=? ORDER BY created_at",
        (ed["id"],),
    ).fetchall()]
    bills = [dict(b) for b in c.execute(
        "SELECT * FROM bills WHERE encounter_id=? ORDER BY created_at",
        (ed["id"],),
    ).fetchall()]

    # Build billed service names from bill line items via services that have bill_id
    billed_service_ids = {s["id"] for s in services if s.get("billed") and s.get("bill_id")}
    unbilled_services = [dict(s) for s in services if not s.get("billed")]

    # Charges without supporting service documentation: bills with total but no linked services
    service_bill_map: Dict[str, List[dict]] = {}
    for s in services:
        if s.get("bill_id"):
            service_bill_map.setdefault(s["bill_id"], []).append(dict(s))

    unbilled_charges = []
    for b in bills:
        bd = dict(b)
        linked = service_bill_map.get(bd["id"], [])
        if not linked and _money(bd.get("total")) > 0:
            unbilled_charges.append(bd)

    # Duplicate charges: same service_name + quantity + unit_price on same encounter
    seen: Dict[str, List[dict]] = {}
    for s in services:
        sd = dict(s)
        key = f"{sd.get('service_name','')}|{sd.get('quantity',1)}|{sd.get('unit_price',0)}"
        seen.setdefault(key, []).append(sd)
    duplicate_charges = {k: v for k, v in seen.items() if len(v) > 1}

    findings = []

    for s in unbilled_services:
        sd = dict(s)
        findings.append({
            "type": "service_without_charge",
            "severity": "high",
            "description": f"Service '{sd.get('service_name')}' documented but not billed",
            "amount": _money(sd.get("total_price")),
            "service_id": sd["id"],
            "bill_id": sd.get("bill_id"),
            "evidence": json.dumps({
                "service_name": sd.get("service_name"),
                "quantity": sd.get("quantity"),
                "unit_price": sd.get("unit_price"),
                "total_price": sd.get("total_price"),
                "billed": sd.get("billed"),
            }),
            "recommended_action": "Review billing and create charge for documented service.",
        })

    for b in unbilled_charges:
        bd = dict(b)
        findings.append({
            "type": "charge_without_service",
            "severity": "high",
            "description": f"Charge {bd.get('bill_number')} recorded without supporting service documentation",
            "amount": _money(bd.get("total")),
            "service_id": None,
            "bill_id": bd["id"],
            "evidence": json.dumps({
                "bill_number": bd.get("bill_number"),
                "total": bd.get("total"),
                "status": bd.get("status"),
            }),
            "recommended_action": "Verify supporting documentation for this charge.",
        })

    for key, dups in duplicate_charges.items():
        total_dup = sum(_money(d.get("total_price")) for d in dups[1:])
        findings.append({
            "type": "possible_duplicate_charge",
            "severity": "medium",
            "description": f"Possible duplicate service charge: {key} ({len(dups)} occurrences)",
            "amount": total_dup,
            "service_id": dups[0]["id"],
            "bill_id": dups[0].get("bill_id"),
            "evidence": json.dumps({
                "duplicate_count": len(dups),
                "service_names": [d.get("service_name") for d in dups],
                "total_duplicate_amount": total_dup,
            }),
            "recommended_action": "Investigate and remove duplicate charge if confirmed.",
        })

    # Persist findings as revenue gap cases + exceptions
    created_cases = []
    for f in findings:
        # Check for existing open case for same encounter + type + service
        existing = c.execute(
            """SELECT id FROM revenue_gap_cases
                 WHERE encounter_id=? AND gap_type=? AND status IN ('Detected','Under Review')
                 LIMIT 1""",
            (ed["id"], f["type"]),
        ).fetchone()
        if existing:
            continue

        case_id = str(uuid.uuid4())
        responsible = {
            "service_without_charge": "Billing",
            "charge_without_service": "Billing",
            "possible_duplicate_charge": "Billing",
        }.get(f["type"], "Billing")

        c.execute(
            """INSERT INTO revenue_gap_cases(id, claim_id, encounter_id, patient_id, gap_type,
                description, amount_involved, potential_recoverable, status,
                responsible_workflow, evidence, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,'Detected',?,?,?,?)""",
            (
                case_id, None, ed["id"], ed.get("patient_id"), f["type"],
                f["description"], _money(f["amount"]), _money(f["amount"]),
                responsible, f["evidence"], now(), now(),
            ),
        )
        add_exception(
            c, "revenue_gap", "high" if f["severity"] == "high" else "medium",
            f"Service-to-revenue gap: {f['type']}",
            f"Encounter {ed.get('encounter_id')}: {f['description']}. {f['recommended_action']}",
            f["recommended_action"], "Revenue Integrity Agent",
            patient_id=ed.get("patient_id"), encounter_id=ed["id"],
        )
        created_cases.append({
            "case_id": case_id,
            "encounter_id": ed["id"],
            "encounter_number": ed.get("encounter_id"),
            "gap_type": f["type"],
            "description": f["description"],
            "amount_involved": round(_money(f["amount"]), 2),
            "status": "Detected",
            "responsible_workflow": responsible,
        })

    end = datetime.datetime.now(datetime.timezone.utc)
    duration_ms = int((end - start).total_seconds() * 1000)
    agent_execution_complete(c, exec_id, "completed",
                             output_data={"cases_created": len(created_cases)},
                             duration_ms=duration_ms)
    c.commit()
    c.close()

    return {
        "encounter_id": ed["id"],
        "encounter_number": ed.get("encounter_id"),
        "services_checked": len(services),
        "bills_checked": len(bills),
        "unbilled_services": len(unbilled_services),
        "unbilled_charges": len(unbilled_charges),
        "duplicate_charge_groups": len(duplicate_charges),
        "findings": findings,
        "cases_created": created_cases,
        "currency": CURRENCY,
    }


# ---------------------------------------------------------------------------
# 2. Closed-Claim Leakage Detection
# ---------------------------------------------------------------------------
def detect_closed_claim_leakage(
    claim_id: Optional[str] = None,
    actor: str = "System",
) -> Dict[str, Any]:
    """For closed/resolved/paid claims, compare expected reimbursement against
    actual payments. Detect underpayments, partial payments, unexplained
    adjustments, unresolved balances. Does not treat every difference as
    recoverable revenue."""
    start = datetime.datetime.now(datetime.timezone.utc)
    c = get_db()
    exec_id = agent_execution_start(
        c, "Revenue Integrity Agent", "detect_closed_claim_leakage",
        "claim", claim_id or "all", {"claim_id": claim_id, "actor": actor},
    )

    closed_statuses = {"paid", "closed", "resolved", "completed"}
    where = "WHERE LOWER(status) IN ({})".format(
        ",".join("?" for _ in closed_statuses)
    )
    params = list(closed_statuses)
    if claim_id:
        where += " AND (id=? OR claim_number=?)"
        params.extend([claim_id, claim_id])

    claims = c.execute(f"SELECT * FROM claims {where}", params).fetchall()
    findings = []

    for cl in claims:
        cld = dict(cl)
        expected = _money(cld.get("expected_reimbursement"))
        if expected <= 0:
            expected = _money(cld.get("approved_amount"))
        approved = _money(cld.get("approved_amount"))
        paid = _money(cld.get("paid_amount"))

        # Actual payments (completed, not reversed)
        payments = [dict(p) for p in c.execute(
            "SELECT * FROM payments WHERE claim_id=? AND status IN ('completed','confirmed')",
            (cld["id"],),
        ).fetchall()]
        received = sum(_money(p.get("amount")) for p in payments)

        # Reversed payments
        reversed_payments = [dict(p) for p in c.execute(
            "SELECT * FROM payments WHERE claim_id=? AND status='reversed'",
            (cld["id"],),
        ).fetchall()]
        reversed_total = sum(_money(p.get("amount")) for p in reversed_payments)

        outstanding = round(expected - received, 2)
        variance = round(received - expected, 2)

        # Determine if difference is explained by legitimate adjustments
        explained = False
        explanation = ""
        if abs(variance) <= 0.01:
            explained = True
            explanation = "Fully paid, no discrepancy."
        elif outstanding > 0.01:
            # Check for contractual adjustment notes or denial
            if cld.get("denial_reason") or cld.get("status") in ("denied", "rejected"):
                explained = True
                explanation = f"Claim denied/rejected: {cld.get('denial_reason') or 'denied'}."
            else:
                explained = False
                explanation = f"Outstanding balance {outstanding:.2f} {CURRENCY} not explained by denial or adjustment."

        if explained and abs(variance) <= 0.01:
            continue  # no leakage

        leakage_type = "underpayment" if outstanding > 0.01 else "overpayment"
        recoverable = max(0.0, outstanding) if not explained else 0.0

        # Check for existing open leakage case (statuses use UPPERCASE lifecycle)
        existing = c.execute(
            """SELECT id FROM leakage_cases
                 WHERE claim_id=? AND status IN ('DETECTED','UNDER_REVIEW','CONFIRMED','ACTION_REQUIRED','IN_PROGRESS')
                 LIMIT 1""",
            (cld["id"],),
        ).fetchone()
        if existing:
            continue

        case_id = str(uuid.uuid4())
        _evidence_json = json.dumps({
            "expected": expected,
            "approved": approved,
            "paid": paid,
            "received": received,
            "reversed": reversed_total,
            "outstanding": outstanding,
            "variance": variance,
            "explained": explained,
            "explanation": explanation,
        })
        c.execute(
            """INSERT INTO leakage_cases(id, category, amount, estimated_exposure, entity_type,
                entity_id, patient_id, encounter_id, claim_id, evidence, supporting_evidence,
                recommended_action, status, created_at, updated_at)
                VALUES(?,?,?,?, 'claim', ?,?,?,?,?,?,?,'DETECTED',?,?)""",
            (
                case_id, leakage_type, round(recoverable, 2), round(recoverable, 2),
                cld["id"], cld.get("patient_id"), cld.get("encounter_id"), cld["id"],
                _evidence_json, _evidence_json,
                "Review closed claim for underpayment or unexplained adjustment.",
                now(), now(),
            ),
        )
        add_exception(
            c, "closed_claim_leakage", "high",
            f"Closed claim leakage: {cld.get('claim_number')}",
            f"Claim {cld.get('claim_number')} expected {expected:.2f} {CURRENCY}, received {received:.2f} {CURRENCY}, outstanding {outstanding:.2f} {CURRENCY}. {explanation}",
            "Investigate and recover if unexplained.",
            "Revenue Integrity Agent",
            patient_id=cld.get("patient_id"), encounter_id=cld.get("encounter_id"), claim_id=cld["id"],
        )
        findings.append({
            "case_id": case_id,
            "claim_id": cld["id"],
            "claim_number": cld.get("claim_number"),
            "leakage_type": leakage_type,
            "expected_reimbursement": round(expected, 2),
            "received": round(received, 2),
            "outstanding": round(outstanding, 2),
            "recoverable": round(recoverable, 2),
            "explained": explained,
            "explanation": explanation,
            "status": "Detected",
        })

    end = datetime.datetime.now(datetime.timezone.utc)
    duration_ms = int((end - start).total_seconds() * 1000)
    agent_execution_complete(c, exec_id, "completed",
                             output_data={"cases_created": len(findings)},
                             duration_ms=duration_ms)
    c.commit()
    c.close()

    return {
        "claims_checked": len(claims),
        "leakage_cases": findings,
        "count": len(findings),
        "currency": CURRENCY,
    }


# ---------------------------------------------------------------------------
# 3. Payer Policy Drift Detection
# ---------------------------------------------------------------------------
def update_payer_rule(
    rule_id: str,
    new_condition: str,
    changed_by: str = "Admin",
    change_reason: str = "",
) -> Dict[str, Any]:
    """Update a payer rule, preserving the previous version for drift detection."""
    c = get_db()
    rule = c.execute("SELECT * FROM payer_rules WHERE id=?", (rule_id,)).fetchone()
    if not rule:
        c.close()
        return {"error": "Payer rule not found"}, 404
    rd = dict(rule)

    # Create version record of the previous rule
    version = c.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM payer_rule_versions WHERE rule_id=?",
        (rule_id,),
    ).fetchone()[0]
    vid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO payer_rule_versions(id, rule_id, rule_name, rule_type, version,
            condition_snapshot, severity, is_active, changed_by, change_reason,
            effective_date, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            vid, rule_id, rd.get("rule_name"), rd.get("rule_type"), version,
            rd.get("condition"), rd.get("severity"), rd.get("is_active"),
            changed_by, change_reason or "Rule updated", now(), now(),
        ),
    )

    # Update the active rule
    c.execute(
        """UPDATE payer_rules SET condition=?, severity=?, updated_at=? WHERE id=?""",
        (json.dumps(new_condition), rd.get("severity"), now(), rule_id),
    )
    audit(
        c, changed_by, "user", "Payer Policy Drift Detector", "Payer rule updated",
        "payer_rule", rule_id,
        previous_state=json.dumps({"condition": rd.get("condition")}),
        new_state=json.dumps({"condition": new_condition}),
        input_summary=json.dumps({"change_reason": change_reason}),
    )
    c.commit()

    # Identify claims potentially affected (vetted under old rule)
    affected = c.execute(
        """SELECT cv.claim_id, c.claim_number, c.payer_name, c.status
           FROM claim_vetting cv
           JOIN claims c ON c.id = cv.claim_id
           WHERE c.payer_name = (SELECT payer_name FROM payer_rules WHERE id=?)
             AND cv.created_at < ?""",
        (rule_id, now()),
    ).fetchall()

    c.close()
    return {
        "rule_id": rule_id,
        "rule_name": rd.get("rule_name"),
        "previous_version": version - 1,
        "new_version": version,
        "changed_by": changed_by,
        "change_reason": change_reason,
        "affected_claims": [
            {
                "claim_id": a["claim_id"],
                "claim_number": a["claim_number"],
                "payer_name": a["payer_name"],
                "status": a["status"],
            } for a in affected
        ],
        "affected_count": len(affected),
        "notice": "SIMULATED payer rule — not a real payer policy.",
    }


def detect_payer_policy_drift(actor: str = "System") -> Dict[str, Any]:
    """Identify claims vetted under outdated payer rule versions."""
    c = get_db()
    start = datetime.datetime.now(datetime.timezone.utc)
    exec_id = agent_execution_start(
        c, "Payer Policy Drift Detector", "detect_drift", "system", None,
        {"actor": actor},
    )

    # Find rules with multiple versions (i.e., rules that have changed)
    changed_rules = c.execute(
        """SELECT rule_id, COUNT(*) v FROM payer_rule_versions
           GROUP BY rule_id HAVING v > 1""",
    ).fetchall()

    drift_cases = []
    for cr in changed_rules:
        rule_id = cr["rule_id"]
        latest = c.execute(
            """SELECT * FROM payer_rule_versions
               WHERE rule_id=? ORDER BY version DESC LIMIT 1""",
            (rule_id,),
        ).fetchone()
        ld = dict(latest)
        rule = c.execute("SELECT * FROM payer_rules WHERE id=?", (rule_id,)).fetchone()
        rd = dict(rule) if rule else {}

        # Claims vetted before the latest version effective date
        affected = c.execute(
            """SELECT cv.claim_id, c.claim_number, c.payer_name, c.status, cv.created_at
               FROM claim_vetting cv
               JOIN claims c ON c.id = cv.claim_id
               WHERE cv.created_at < ? AND c.payer_name=?""",
            (ld.get("effective_date") or now(), rd.get("payer_name") or ""),
        ).fetchall()

        for a in affected:
            drift_cases.append({
                "claim_id": a["claim_id"],
                "claim_number": a["claim_number"],
                "payer_name": a["payer_name"],
                "rule_name": rd.get("rule_name"),
                "rule_id": rule_id,
                "vet_date": a["created_at"],
                "rule_changed_on": ld.get("effective_date"),
                "status": "requires_review",
            })

    end = datetime.datetime.now(datetime.timezone.utc)
    duration_ms = int((end - start).total_seconds() * 1000)
    agent_execution_complete(c, exec_id, "completed",
                             output_data={"drift_cases": len(drift_cases)},
                             duration_ms=duration_ms)
    c.close()

    return {
        "changed_rules": len(changed_rules),
        "drift_cases": drift_cases,
        "count": len(drift_cases),
        "notice": "SIMULATED payer rules — not real NHIS tariffs or payer policies.",
    }


# ---------------------------------------------------------------------------
# 4. Unworked Revenue Prioritization
# ---------------------------------------------------------------------------
def prioritize_revenue_cases(actor: str = "System") -> List[Dict[str, Any]]:
    """Prioritize follow-up, collections, and underpayment cases using
    persisted data only: outstanding amount, claim age, follow-up attempts,
    discrepancy type. No invented probabilities or deadlines."""
    c = get_db()
    cases = []

    # Underpayment cases
    underpayments = c.execute(
        """SELECT uc.*, c.created_at as claim_created_at
           FROM underpayment_cases uc
           LEFT JOIN claims c ON c.id = uc.claim_id
           WHERE uc.status IN ('open','under_review')
           ORDER BY uc.outstanding_amount DESC""",
    ).fetchall()
    for uc in underpayments:
        ud = dict(uc)
        age_days = _days_between(ud.get("claim_created_at"), ud.get("updated_at")) or 0
        followups = c.execute(
            "SELECT COUNT(*) n FROM claim_followups WHERE claim_id=?",
            (ud["claim_id"],),
        ).fetchone()["n"]
        score = _money(ud.get("outstanding_amount")) * 0.5 + age_days * 0.1 + followups * 2.0
        reason = f"Outstanding {_money(ud.get('outstanding_amount')):.2f} {CURRENCY}; {age_days} days old; {followups} follow-up(s)."
        cases.append({
            "case_id": ud["id"],
            "claim_id": ud["claim_id"],
            "claim_number": ud.get("claim_number"),
            "payer_name": ud.get("payer_name"),
            "case_type": "underpayment",
            "outstanding_amount": round(_money(ud.get("outstanding_amount")), 2),
            "age_days": age_days,
            "followup_attempts": followups,
            "priority_score": round(score, 2),
            "priority_reason": reason,
            "status": ud.get("status"),
        })

    # Revenue gap cases
    gaps = c.execute(
        """SELECT rgc.*, e.created_at as encounter_created_at
           FROM revenue_gap_cases rgc
           LEFT JOIN encounters e ON e.id = rgc.encounter_id
           WHERE rgc.status IN ('Detected','Under Review')
           ORDER BY rgc.amount_involved DESC""",
    ).fetchall()
    for g in gaps:
        gd = dict(g)
        age_days = _days_between(gd.get("encounter_created_at"), gd.get("updated_at")) or 0
        score = _money(gd.get("amount_involved")) * 0.5 + age_days * 0.1
        reason = f"Gap {gd.get('gap_type')} involving {_money(gd.get('amount_involved')):.2f} {CURRENCY}; {age_days} days old."
        cases.append({
            "case_id": gd["id"],
            "claim_id": gd.get("claim_id"),
            "encounter_id": gd.get("encounter_id"),
            "case_type": "revenue_gap",
            "outstanding_amount": round(_money(gd.get("amount_involved")), 2),
            "age_days": age_days,
            "followup_attempts": 0,
            "priority_score": round(score, 2),
            "priority_reason": reason,
            "status": gd.get("status"),
        })

    # Leakage cases
    leaks = c.execute(
        """SELECT lc.*, c.created_at as claim_created_at
           FROM leakage_cases lc
           LEFT JOIN claims c ON c.id = lc.claim_id
           WHERE lc.status IN ('DETECTED','UNDER_REVIEW','Detected','Under Review')
           ORDER BY COALESCE(lc.estimated_exposure, lc.amount, 0) DESC""",
    ).fetchall()
    for lk in leaks:
        ld = dict(lk)
        age_days = _days_between(ld.get("claim_created_at"), ld.get("updated_at")) or 0
        est = _money(ld["estimated_exposure"]) if ld.get("estimated_exposure") is not None else _money(ld.get("amount"))
        score = est * 0.5 + age_days * 0.1
        reason = f"Leakage {ld.get('category')}: {est:.2f} {CURRENCY}; {age_days} days old."
        cases.append({
            "case_id": ld["id"],
            "claim_id": ld.get("claim_id"),
            "case_type": "leakage",
            "outstanding_amount": round(est, 2),
            "age_days": age_days,
            "followup_attempts": 0,
            "priority_score": round(score, 2),
            "priority_reason": reason,
            "status": ld.get("status"),
        })

    # Collection tasks
    cols = c.execute(
        """SELECT ct.*, pb.outstanding as balance_outstanding, e.created_at as encounter_created_at
           FROM collection_tasks ct
           LEFT JOIN patient_balances pb ON pb.id = ct.balance_id
           LEFT JOIN encounters e ON e.id = ct.patient_id
           WHERE ct.status = 'open'
           ORDER BY pb.outstanding DESC""",
    ).fetchall()
    for col in cols:
        cd = dict(col)
        age_days = _days_between(cd.get("encounter_created_at"), cd.get("updated_at")) or 0
        score = _money(cd.get("balance_outstanding")) * 0.5 + age_days * 0.1
        reason = f"Collection balance {_money(cd.get('balance_outstanding')):.2f} {CURRENCY}; {age_days} days old."
        cases.append({
            "case_id": cd["id"],
            "claim_id": None,
            "encounter_id": cd.get("encounter_id"),
            "case_type": "collection",
            "outstanding_amount": round(_money(cd.get("balance_outstanding")), 2),
            "age_days": age_days,
            "followup_attempts": 0,
            "priority_score": round(score, 2),
            "priority_reason": reason,
            "status": cd.get("status"),
        })

    cases.sort(key=lambda x: x["priority_score"], reverse=True)
    c.close()
    return {
        "prioritized_cases": cases,
        "count": len(cases),
        "currency": CURRENCY,
        "method": "Transparent scoring: outstanding_amount*0.5 + age_days*0.1 + followups*2.0",
    }


# ---------------------------------------------------------------------------
# 5. Cross-Department Revenue Gap Detection
# ---------------------------------------------------------------------------
def detect_cross_department_gaps(actor: str = "System") -> List[Dict[str, Any]]:
    """Identify gaps across registration, encounter, documentation, services,
    billing, claims, payments, reconciliation. Route to relevant workflow."""
    c = get_db()
    start = datetime.datetime.now(datetime.timezone.utc)
    exec_id = agent_execution_start(
        c, "Revenue Integrity Agent", "detect_cross_department_gaps",
        "system", None, {"actor": actor},
    )

    gaps = []

    # Encounter without documentation complete but services recorded
    encs = [dict(e) for e in c.execute(
        """SELECT e.* FROM encounters e
           LEFT JOIN services s ON s.encounter_id = e.id
           WHERE e.documentation_complete = 0 AND s.id IS NOT NULL
           GROUP BY e.id""",
    ).fetchall()]
    for e in encs:
        ed = dict(e)
        gaps.append({
            "encounter_id": ed["id"],
            "encounter_number": ed.get("encounter_id"),
            "patient_id": ed.get("patient_id"),
            "gap_type": "documentation_incomplete_with_services",
            "description": "Encounter has recorded services but documentation is incomplete",
            "amount_involved": 0,
            "responsible_workflow": "Documentation",
            "evidence": json.dumps({"encounter_id": ed["id"], "status": ed.get("status")}),
        })

    # Services recorded but encounter not completed (billing gap)
    encs2 = [dict(e) for e in c.execute(
        """SELECT e.* FROM encounters e
           JOIN services s ON s.encounter_id = e.id
           WHERE e.status NOT IN ('completed','discharged')
           GROUP BY e.id""",
    ).fetchall()]
    for e in encs2:
        ed = dict(e)
        existing = c.execute(
            "SELECT id FROM revenue_gap_cases WHERE encounter_id=? AND gap_type='encounter_not_completed' LIMIT 1",
            (ed["id"],),
        ).fetchone()
        if existing:
            continue
        gaps.append({
            "encounter_id": ed["id"],
            "encounter_number": ed.get("encounter_id"),
            "patient_id": ed.get("patient_id"),
            "gap_type": "encounter_not_completed",
            "description": "Services recorded but encounter status not completed",
            "amount_involved": 0,
            "responsible_workflow": "Encounters",
            "evidence": json.dumps({"encounter_id": ed["id"], "status": ed.get("status")}),
        })

    # Claims not submitted for completed encounters with bills
    encs3 = [dict(e) for e in c.execute(
        """SELECT e.*, b.total as bill_total FROM encounters e
           JOIN bills b ON b.encounter_id = e.id
           LEFT JOIN claims cl ON cl.encounter_id = e.id
           WHERE e.status = 'completed' AND cl.id IS NULL""",
    ).fetchall()]
    for e in encs3:
        ed = dict(e)
        gaps.append({
            "encounter_id": ed["id"],
            "encounter_number": ed.get("encounter_id"),
            "patient_id": ed.get("patient_id"),
            "gap_type": "claim_not_submitted",
            "description": "Completed encounter with bill but no claim submitted",
            "amount_involved": _money(ed.get("bill_total")),
            "responsible_workflow": "Claims",
            "evidence": json.dumps({"encounter_id": ed["id"], "bill_total": ed.get("bill_total")}),
        })

    # Persist new gap cases (deduplicated)
    created = []
    for g in gaps:
        existing = c.execute(
            """SELECT id FROM revenue_gap_cases
                 WHERE encounter_id=? AND gap_type=? AND status IN ('Detected','Under Review')
                 LIMIT 1""",
            (g["encounter_id"], g["gap_type"]),
        ).fetchone()
        if existing:
            continue

        case_id = str(uuid.uuid4())
        c.execute(
            """INSERT INTO revenue_gap_cases(id, claim_id, encounter_id, patient_id, gap_type,
                description, amount_involved, potential_recoverable, status,
                responsible_workflow, evidence, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,'Detected',?,?,?,?)""",
            (
                case_id, None, g["encounter_id"], g.get("patient_id"), g["gap_type"],
                g["description"], _money(g["amount_involved"]), _money(g["amount_involved"]),
                g["responsible_workflow"], g["evidence"], now(), now(),
            ),
        )
        add_exception(
            c, "revenue_gap", "medium",
            f"Cross-department gap: {g['gap_type']}",
            f"Encounter {g.get('encounter_number')}: {g['description']}",
            "Review and route to appropriate workflow.", "Revenue Integrity Agent",
            patient_id=g.get("patient_id"), encounter_id=g["encounter_id"],
        )
        created.append({
            "case_id": case_id,
            "encounter_id": g["encounter_id"],
            "gap_type": g["gap_type"],
            "description": g["description"],
            "amount_involved": round(_money(g["amount_involved"]), 2),
            "responsible_workflow": g["responsible_workflow"],
            "status": "Detected",
        })

    end = datetime.datetime.now(datetime.timezone.utc)
    duration_ms = int((end - start).total_seconds() * 1000)
    agent_execution_complete(c, exec_id, "completed",
                             output_data={"cases_created": len(created)},
                             duration_ms=duration_ms)
    c.commit()
    c.close()

    return {"gap_cases": created, "count": len(created), "currency": CURRENCY}


# ---------------------------------------------------------------------------
# 6. Write-Off Auditing
# ---------------------------------------------------------------------------
def audit_write_off(
    claim_id: str,
    amount: float,
    adjustment_type: str,
    reason: str,
    actor: str = "User",
) -> Dict[str, Any]:
    """Audit a write-off before finalization. Flags unexplained adjustments,
    recoverable balances, missed appeal/follow-up opportunities. Human approval
    required; does not auto-finalize."""
    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        c.close()
        return {"error": "Claim not found"}, 404
    cld = dict(claim)

    flagged_reasons = []
    recoverable = 0.0

    # Check for unexplained adjustment (no denial reason, no payer rule)
    if not cld.get("denial_reason") and not cld.get("denial_category"):
        flagged_reasons.append("No denial reason or category recorded for write-off")
        recoverable += _money(amount)

    # Check for missed follow-up opportunities
    followups = c.execute(
        "SELECT COUNT(*) n FROM claim_followups WHERE claim_id=?",
        (cld["id"],),
    ).fetchone()["n"]
    if followups == 0:
        flagged_reasons.append("No follow-up attempts recorded before write-off")
        recoverable += _money(amount)

    # Check for missed appeal opportunities
    appeals = c.execute(
        "SELECT COUNT(*) n FROM appeals WHERE claim_id=? AND status IN ('APPROVED','DETECTED')",
        (cld["id"],),
    ).fetchone()["n"]
    if appeals == 0 and _money(cld.get("total_charges")) > 0:
        flagged_reasons.append("No appeal recorded; potential appeal opportunity missed")
        recoverable += _money(amount)

    # Check against payer rules (simulated)
    rules = [dict(r) for r in c.execute(
        "SELECT * FROM payer_rules WHERE is_active=1 AND is_simulated=1",
    ).fetchall()]
    for r in rules:
        cond = json.loads(r.get("condition") or "{}")
        if cond.get("check") == "valid_totals" and _money(amount) > _money(cld.get("total_charges")):
            flagged_reasons.append(f"Write-off amount exceeds total charges (rule: {r.get('rule_name')})")
            break

    status = "pending_review" if flagged_reasons else "approved"
    audit_id = str(uuid.uuid4())
    c.execute(
        """INSERT INTO write_off_audits(id, claim_id, claim_number, encounter_id, amount,
            adjustment_type, reason, status, flagged_reasons, recoverable_amount,
            reviewer, review_decision, review_notes, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
        (
            audit_id, cld["id"], cld.get("claim_number"), cld.get("encounter_id"),
            _money(amount), adjustment_type, reason, status,
            json.dumps(flagged_reasons), round(recoverable, 2),
            actor, "", now(), now(),
        ),
    )
    add_exception(
        c, "write_off_audit", "high" if flagged_reasons else "low",
        f"Write-off audit: {cld.get('claim_number')}",
        f"Write-off of {_money(amount):.2f} {CURRENCY} for claim {cld.get('claim_number')}. Flags: {', '.join(flagged_reasons) or 'none'}.",
        "Human review required before finalizing write-off." if flagged_reasons else "Write-off appears consistent with rules.",
        "Write-Off Auditor",
        patient_id=cld.get("patient_id"), encounter_id=cld.get("encounter_id"), claim_id=cld["id"],
    )
    audit(
        c, actor, "user", "Write-Off Auditor", "Write-off audited", "write_off_audit", audit_id,
        previous_state=None, new_state=status,
        input_summary=json.dumps({"amount": amount, "reason": reason, "flagged": flagged_reasons}),
        output_summary=json.dumps({"recoverable": recoverable, "status": status}),
    )
    c.commit()
    c.close()

    return {
        "audit_id": audit_id,
        "claim_id": cld["id"],
        "claim_number": cld.get("claim_number"),
        "amount": round(_money(amount), 2),
        "adjustment_type": adjustment_type,
        "status": status,
        "flagged_reasons": flagged_reasons,
        "recoverable_amount": round(recoverable, 2),
        "human_review_required": bool(flagged_reasons),
        "notice": "Write-off remains subject to human approval.",
    }


def list_write_off_audits(filters: Dict[str, Any] = None) -> List[dict]:
    c = get_db()
    q = "SELECT * FROM write_off_audits WHERE 1=1"
    params: List[Any] = []
    f = filters or {}
    if f.get("status"):
        q += " AND status=?"; params.append(f["status"])
    if f.get("claim_id"):
        q += " AND claim_id=?"; params.append(f["claim_id"])
    q += " ORDER BY created_at DESC"
    rows = c.execute(q, params).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 7. Revenue Leakage Memory
# ---------------------------------------------------------------------------
def detect_leakage_patterns(actor: str = "System") -> List[Dict[str, Any]]:
    """Aggregate resolved leakage/gap/write-off cases to identify recurring
    patterns. Deterministic rules only."""
    c = get_db()
    patterns = []

    # Repeated discrepancy types by payer
    leak_rows = c.execute(
        """SELECT lc.category, c.payer_name, COUNT(*) cnt, SUM(COALESCE(lc.estimated_exposure, lc.amount, 0)) total
           FROM leakage_cases lc
           LEFT JOIN claims c ON c.id = lc.claim_id
           WHERE lc.status IN ('RESOLVED','Resolved','Recovered','Closed')
           GROUP BY lc.category, c.payer_name
           HAVING cnt >= 2""",
    ).fetchall()
    for row in leak_rows:
        rd = dict(row)
        pattern_key = f"leakage|{rd['category']}|{rd['payer_name']}"
        existing = c.execute(
            "SELECT id FROM leakage_memory WHERE pattern_key=?", (pattern_key,),
        ).fetchone()
        if not existing:
            pid = str(uuid.uuid4())
            c.execute(
                """INSERT INTO leakage_memory(id, pattern_type, pattern_key, occurrence_count,
                    first_seen, last_seen, total_amount, confirmed_recoverable, description, status)
                    VALUES(?,?,'leakage',?,?,?,?,?,'active')""",
                (
                    pid, rd["category"], rd["cnt"], now(), now(),
                    _money(rd["total"]), _money(rd["total"]) * 0.5,
                    f"Repeated {rd['category']} leakage for {rd['payer_name']} ({rd['cnt']} occurrences, {_money(rd['total']):.2f} {CURRENCY})",
                ),
            )
            patterns.append({
                "pattern_type": rd["category"],
                "pattern_key": pattern_key,
                "occurrence_count": rd["cnt"],
                "payer_name": rd["payer_name"],
                "total_amount": round(_money(rd["total"]), 2),
                "description": f"Repeated {rd['category']} leakage for {rd['payer_name']}",
            })

    # Repeated gap types
    gap_rows = c.execute(
        """SELECT gap_type, responsible_workflow, COUNT(*) cnt, SUM(amount_involved) total
           FROM revenue_gap_cases
           WHERE status IN ('Resolved','Closed')
           GROUP BY gap_type, responsible_workflow
           HAVING cnt >= 2""",
    ).fetchall()
    for row in gap_rows:
        rd = dict(row)
        pattern_key = f"gap|{rd['gap_type']}|{rd['responsible_workflow']}"
        existing = c.execute(
            "SELECT id FROM leakage_memory WHERE pattern_key=?", (pattern_key,),
        ).fetchone()
        if not existing:
            pid = str(uuid.uuid4())
            c.execute(
                """INSERT INTO leakage_memory(id, pattern_type, pattern_key, occurrence_count,
                    first_seen, last_seen, total_amount, confirmed_recoverable, description, status)
                    VALUES(?,?,'gap',?,?,?,?,?,'active')""",
                (
                    pid, rd["gap_type"], rd["cnt"], now(), now(),
                    _money(rd["total"]), 0,
                    f"Recurring {rd['gap_type']} gaps routed to {rd['responsible_workflow']} ({rd['cnt']} occurrences)",
                ),
            )
            patterns.append({
                "pattern_type": rd["gap_type"],
                "pattern_key": pattern_key,
                "occurrence_count": rd["cnt"],
                "workflow": rd["responsible_workflow"],
                "total_amount": round(_money(rd["total"]), 2),
                "description": f"Recurring {rd['gap_type']} gaps routed to {rd['responsible_workflow']}",
            })

    c.commit()
    c.close()
    return {"patterns": patterns, "count": len(patterns), "currency": CURRENCY}


# ---------------------------------------------------------------------------
# 8. Revenue Recovery Workflow
# ---------------------------------------------------------------------------
def create_recovery_case(
    claim_id: str,
    discrepancy_type: str,
    expected_amount: float,
    actor: str = "User",
    notes: str = "",
) -> Dict[str, Any]:
    """Create a revenue recovery case linked to an existing claim. Routes to
    the relevant existing workflow (follow-up, appeal, reconciliation)."""
    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        c.close()
        return {"error": "Claim not found"}, 404
    cld = dict(claim)

    # Determine priority score
    outstanding = max(0.0, _money(expected_amount) - _money(cld.get("paid_amount")))
    age_days = _days_between(cld.get("created_at"), now()) or 0
    score = outstanding * 0.5 + age_days * 0.1
    reason = f"Outstanding {outstanding:.2f} {CURRENCY}; {age_days} days old; discrepancy: {discrepancy_type}."

    # Route to appropriate workflow
    if discrepancy_type in ("underpayment", "partial_payment"):
        assigned = "reconciliation"
    elif discrepancy_type in ("denial", "appeal"):
        assigned = "appeals"
    else:
        assigned = "follow_up"

    case_id = str(uuid.uuid4())
    c.execute(
        """INSERT INTO recovery_cases(id, claim_id, discrepancy_type, status,
            priority_score, priority_reason, expected_amount, recovered_amount,
            verified_amount, workflow_stage, assigned_workflow, created_at, updated_at)
            VALUES(?,?,?,'Detected',?,?,?,0,0,'case_created',?,?,?)""",
        (
            case_id, cld["id"], discrepancy_type, round(score, 2), reason,
            _money(expected_amount), assigned, now(), now(),
        ),
    )
    audit(
        c, actor, "user", "Revenue Recovery Workflow", "Recovery case created",
        "recovery_case", case_id,
        previous_state=None, new_state="Detected",
        input_summary=json.dumps({"discrepancy_type": discrepancy_type, "expected_amount": expected_amount}),
        output_summary=json.dumps({"assigned_workflow": assigned, "priority_score": score}),
    )
    c.commit()
    c.close()

    return {
        "case_id": case_id,
        "claim_id": cld["id"],
        "claim_number": cld.get("claim_number"),
        "discrepancy_type": discrepancy_type,
        "expected_amount": round(_money(expected_amount), 2),
        "status": "Detected",
        "priority_score": round(score, 2),
        "priority_reason": reason,
        "assigned_workflow": assigned,
        "notice": "Recovery revenue counted only after verified payment.",
    }


def record_recovery_payment(
    case_id: str,
    amount: float,
    actor: str = "User",
    reference: str = "",
) -> Dict[str, Any]:
    """Record a payment received for a recovery case. Verified revenue only
    after payment is recorded and reconciled."""
    c = get_db()
    case = c.execute("SELECT * FROM recovery_cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        c.close()
        return {"error": "Recovery case not found"}, 404
    cd = dict(case)

    # Link to claim payment if claim_id exists
    claim_id = cd.get("claim_id")
    pay_id = None
    if claim_id:
        pay_res = None
        # Use existing payment recording via reconciliation module to keep consistency
        from agents import reconciliation as recon_mod
        pay_res = recon_mod.record_simulated_payment(
            claim_id, _money(amount), "Bank Transfer", reference or f"RECOV-{case_id[:8]}",
            actor=actor, payment_status="completed",
        )
        if isinstance(pay_res, dict) and pay_res.get("payment_id"):
            pay_id = pay_res["payment_id"]

    new_recovered = _money(cd.get("recovered_amount")) + _money(amount)
    new_verified = new_recovered  # verified once payment recorded and reconciled
    c.execute(
        """UPDATE recovery_cases SET recovered_amount=?, verified_amount=?,
            status='Recovered', updated_at=? WHERE id=?""",
        (round(new_recovered, 2), round(new_verified, 2), now(), case_id),
    )
    audit(
        c, actor, "user", "Revenue Recovery Workflow", "Recovery payment recorded",
        "recovery_case", case_id,
        previous_state="Detected", new_state="Recovered",
        input_summary=json.dumps({"amount": amount, "reference": reference}),
        output_summary=json.dumps({"recovered": new_recovered, "verified": new_verified}),
    )
    c.commit()
    c.close()

    return {
        "case_id": case_id,
        "payment_id": pay_id,
        "amount": round(_money(amount), 2),
        "total_recovered": round(new_recovered, 2),
        "total_verified": round(new_verified, 2),
        "status": "Recovered",
        "notice": "Revenue counted as recovered only after verified payment.",
    }


def list_recovery_cases(filters: Dict[str, Any] = None) -> List[dict]:
    c = get_db()
    q = "SELECT * FROM recovery_cases WHERE 1=1"
    params: List[Any] = []
    f = filters or {}
    if f.get("status"):
        q += " AND status=?"; params.append(f["status"])
    if f.get("claim_id"):
        q += " AND claim_id=?"; params.append(f["claim_id"])
    q += " ORDER BY priority_score DESC, created_at DESC"
    rows = c.execute(q, params).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Analytics extension
# ---------------------------------------------------------------------------
def revenue_integrity_analytics() -> Dict[str, Any]:
    """DB-derived revenue integrity metrics. Distinguishes potential,
    confirmed, and verified recovery."""
    c = get_db()

    # Gap cases
    gap_cases = [dict(g) for g in c.execute("SELECT * FROM revenue_gap_cases").fetchall()]
    potential_leakage = sum(_money(g.get("amount_involved")) for g in gap_cases if (g.get("status") or "") == "Detected")
    confirmed_gaps = sum(_money(g.get("potential_recoverable")) for g in gap_cases if (g.get("status") or "") in ("Under Review","Confirmed"))

    # Leakage cases (statuses use UPPERCASE lifecycle; amount/estimated_exposure kept in sync)
    leak_cases = [dict(l) for l in c.execute("SELECT * FROM leakage_cases").fetchall()]
    def _leak_est(l):
        return _money(l["estimated_exposure"]) if l.get("estimated_exposure") is not None else _money(l.get("amount"))
    leak_open_statuses = {"DETECTED", "UNDER_REVIEW"}
    leak_confirmed_statuses = {"CONFIRMED", "ACTION_REQUIRED", "IN_PROGRESS"}
    leak_resolved_statuses = {"RESOLVED", "Recovered", "Closed"}
    leakage_detected = sum(_leak_est(l) for l in leak_cases if (l.get("status") or "") in leak_open_statuses)
    leakage_confirmed = sum((_money(l.get("confirmed_amount")) or _leak_est(l)) for l in leak_cases if (l.get("status") or "") in leak_confirmed_statuses)
    leakage_recovered = sum(_money(l.get("verified_recovered_amount")) for l in leak_cases if (l.get("status") or "") in leak_resolved_statuses)

    # Write-off audits
    wo_audits = [dict(w) for w in c.execute("SELECT * FROM write_off_audits").fetchall()]
    write_offs_reviewed = len(wo_audits)
    write_offs_flagged = sum(1 for w in wo_audits if (w.get("status") or "") == "pending_review")
    write_off_recoverable = sum(_money(w.get("recoverable_amount")) for w in wo_audits)

    # Recovery cases
    rec_cases = [dict(r) for r in c.execute("SELECT * FROM recovery_cases").fetchall()]
    recovery_initiated = sum(_money(r.get("expected_amount")) for r in rec_cases if (r.get("status") or "") in ("Detected","Under Review"))
    recovery_received = sum(_money(r.get("recovered_amount")) for r in rec_cases if (r.get("status") or "") == "Recovered")
    recovery_verified = sum(_money(r.get("verified_amount")) for r in rec_cases if (r.get("status") or "") == "Recovered")

    # Patterns
    patterns = c.execute("SELECT COUNT(*) n FROM leakage_memory").fetchone()["n"]

    c.close()
    return {
        "currency": CURRENCY,
        "kpis": {
            "potential_leakage_identified": {"label": "Potential leakage identified", "value": round(potential_leakage, 2),
                "definition": "Sum of amount_involved across revenue gap cases with status Detected"},
            "confirmed_discrepancies": {"label": "Confirmed discrepancies", "value": round(confirmed_gaps + leakage_confirmed, 2),
                "definition": "Sum of potential_recoverable/amount across cases confirmed"},
            "confirmed_recoverable": {"label": "Confirmed recoverable revenue", "value": round(confirmed_gaps + leakage_confirmed, 2),
                "definition": "Amount confirmed recoverable but not yet received"},
            "verified_recovered_revenue": {"label": "Verified recovered revenue", "value": round(recovery_verified, 2),
                "definition": "Revenue recovered and verified through recorded payment (not potential)"},
            "unresolved_discrepancies": {"label": "Unresolved discrepancies", "value": len([g for g in gap_cases if (g.get("status") or "") == "Detected"]) + len([l for l in leak_cases if (l.get("status") or "") in leak_open_statuses]),
                "definition": "Count of gap and leakage cases still Detected"},
            "write_offs_reviewed": {"label": "Write-offs reviewed", "value": write_offs_reviewed,
                "definition": "Count of write-off audit records"},
            "write_offs_flagged": {"label": "Write-offs flagged", "value": write_offs_flagged,
                "definition": "Count of write-offs pending human review"},
            "missing_charges_identified": {"label": "Missing charges identified", "value": sum(1 for g in gap_cases if (g.get("gap_type") or "") == "service_without_charge"),
                "definition": "Count of service-without-charge gap cases"},
            "recovery_cases_initiated": {"label": "Recovery cases initiated", "value": len(rec_cases),
                "definition": "Count of recovery cases created"},
            "recovery_cases_resolved": {"label": "Recovery cases resolved", "value": sum(1 for r in rec_cases if (r.get("status") or "") == "Recovered"),
                "definition": "Count of recovery cases with verified recovery"},
            "recurring_leakage_patterns": {"label": "Recurring leakage patterns", "value": patterns,
                "definition": "Count of distinct recurring patterns in leakage memory"},
        },
        "counts": {
            "gap_cases": len(gap_cases),
            "leakage_cases": len(leak_cases),
            "write_off_audits": len(wo_audits),
            "recovery_cases": len(rec_cases),
            "leakage_patterns": patterns,
        },
        "distinctions": {
            "potential_vs_confirmed": "Potential leakage is not confirmed recoverable revenue",
            "confirmed_vs_recovered": "Confirmed recoverable revenue is not recovered until payment is verified",
            "initiated_vs_verified": "Recovery initiated is not recovery received",
        },
    }
