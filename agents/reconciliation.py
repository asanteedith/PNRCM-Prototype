"""Reconciliation & Verified Revenue Recovery Agent.

Deterministic reconciliation engine over persisted financial records.
Distinguishes billed, approved, expected, received, reconciled, and verified
amounts. All reconciliation decisions require human review; the agent never
executes irreversible financial actions.
"""
from __future__ import annotations

import json, uuid, datetime
from typing import Any, Dict, List, Tuple, Optional
from db import get_db, now, audit, agent_execution_start, agent_execution_complete, add_exception

CURRENCY = "GHS"

RECONCILIATION_STATUSES = [
    "Awaiting Adjudication",
    "Awaiting Payment",
    "Partially Paid",
    "Fully Paid — Pending Reconciliation",
    "Reconciled",
    "Underpaid",
    "Overpaid",
    "Payment Discrepancy",
    "Requires Human Review",
]

UNDERPAYMENT_REASONS = [
    "approved_but_not_paid",
    "partial_payment",
    "paid_below_expected",
    "unexplained_adjustment",
    "payment_discrepancy",
]


def _money(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _status_for(
    approved_amount: float,
    expected_reimbursement: float,
    total_received: float,
    reconciled: float,
    variance: float,
    has_incomplete: bool,
) -> str:
    if has_incomplete:
        return "Requires Human Review"
    if approved_amount <= 0:
        return "Awaiting Adjudication"
    if total_received <= 0.01:
        return "Awaiting Payment"
    if abs(total_received - expected_reimbursement) <= 0.01:
        if abs(reconciled - expected_reimbursement) <= 0.01:
            if abs(variance) <= 0.01:
                return "Reconciled"
            if variance < -0.01:
                return "Underpaid"
            if variance > 0.01:
                return "Overpaid"
        return "Fully Paid — Pending Reconciliation"
    if total_received < expected_reimbursement - 0.01:
        return "Partially Paid"
    return "Payment Discrepancy"


def reconcile_claim(claim_id: str, actor: str = "System") -> Dict[str, Any]:
    """Reconcile a single claim against its payments and approved amount.

    Calculations:
      Expected Reimbursement = Approved Amount − Valid Adjustments
      Outstanding Balance    = Expected Reimbursement − Reconciled Payments
      Reconciliation Variance= Actual Payments − Expected Reimbursement

    Never auto-modifies financial records to force a match.
    """
    start = datetime.datetime.now(datetime.timezone.utc)
    c = get_db()
    exec_id = agent_execution_start(c, "Reconciliation Agent", "reconcile_claim", "claim", claim_id,
                                    {"claim_id": claim_id, "actor": actor})

    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        agent_execution_complete(c, exec_id, "failed", error_message="Claim not found")
        c.close()
        return {"error": "Claim not found"}, 404
    cld = dict(claim)

    claim_number = cld.get("claim_number") or cld["id"]
    patient_id = cld.get("patient_id")
    payer_name = cld.get("payer_name") or cld.get("payer_type") or "Unknown"
    payer_type = cld.get("payer_type") or ""

    billed_amount = _money(cld.get("total_charges"))
    approved_amount = _money(cld.get("approved_amount"))
    expected_reimbursement = _money(cld.get("expected_reimbursement"))
    if expected_reimbursement <= 0 and approved_amount > 0:
        expected_reimbursement = approved_amount  # no adjustments recorded yet

    # Incomplete-data flag: expected reimbursement cannot be determined
    has_incomplete = False
    if approved_amount > 0 and expected_reimbursement <= 0:
        has_incomplete = True

    # All payments linked to this claim
    payments = [dict(p) for p in c.execute(
        "SELECT * FROM payments WHERE claim_id=? ORDER BY created_at",
        (cld["id"],),
    ).fetchall()]

    # Completed (received) payments only
    received = [p for p in payments if (p.get("status") or "").lower() in ("completed", "confirmed")]
    total_received = sum(_money(p.get("amount")) for p in received)

    # Auto-match received payments that lack a payment_match (idempotent).
    # Matches start as 'unreconciled'; human review confirms them.
    matched_payment_ids = set()
    for p in received:
        pid = p.get("id")
        existing_match = c.execute(
            "SELECT id FROM payment_matches WHERE claim_id=? AND payment_id=?", (cld["id"], pid)
        ).fetchone()
        if not existing_match:
            mid = str(uuid.uuid4())
            c.execute(
                """INSERT INTO payment_matches(id, claim_id, payment_id, matched_amount, match_status,
                    matched_at, matched_by, notes, reconciliation_status, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,'unreconciled',?,?)""",
                (mid, cld["id"], pid, _money(p.get("amount")), "matched", now(), "System",
                 "Auto-matched during reconciliation", now(), now()),
            )
        matched_payment_ids.add(pid)

    # Reconciled payments = matched payments with reconciliation_status == reconciled
    matches = c.execute(
        "SELECT * FROM payment_matches WHERE claim_id=?",
        (cld["id"],),
    ).fetchall()
    reconciled_total = 0.0
    for m in matches:
        md = dict(m)
        if (md.get("reconciliation_status") or "").lower() == "reconciled":
            reconciled_total += _money(md.get("matched_amount"))

    # Duplicate detection: same reference + amount + claim, more than one completed
    refs: Dict[str, List[dict]] = {}
    for p in received:
        ref = p.get("reference") or ""
        key = (ref, round(_money(p.get("amount")), 2))
        refs.setdefault(key, []).append(dict(p))
    duplicate_keys = {k: v for k, v in refs.items() if k[0] and len(v) > 1}

    adjustments_total = 0.0  # no separate adjustments table; tracked via line items if needed

    outstanding_balance = round(expected_reimbursement - reconciled_total, 2)
    variance = round(total_received - expected_reimbursement, 2)

    status = _status_for(approved_amount, expected_reimbursement, total_received, reconciled_total, variance, has_incomplete)

    # Persist or update reconciliation record
    existing = c.execute("SELECT id FROM reconciliations WHERE claim_id=?", (cld["id"],)).fetchone()
    rec_id = existing["id"] if existing else str(uuid.uuid4())

    c.execute(
        """INSERT INTO reconciliations(id, claim_id, claim_number, patient_id, payer_name, payer_type,
            billed_amount, approved_amount, expected_reimbursement, adjustments_total,
            total_payments_received, reconciled_payments, outstanding_balance, variance,
            status, reconciliation_date, reviewed_by, review_decision, review_notes,
            created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
            claim_number=excluded.claim_number, patient_id=excluded.patient_id,
            payer_name=excluded.payer_name, payer_type=excluded.payer_type,
            billed_amount=excluded.billed_amount, approved_amount=excluded.approved_amount,
            expected_reimbursement=excluded.expected_reimbursement,
            adjustments_total=excluded.adjustments_total,
            total_payments_received=excluded.total_payments_received,
            reconciled_payments=excluded.reconciled_payments,
            outstanding_balance=excluded.outstanding_balance, variance=excluded.variance,
            status=excluded.status, reconciliation_date=excluded.reconciliation_date,
            updated_at=excluded.updated_at""",
        (rec_id, cld["id"], claim_number, patient_id, payer_name, payer_type,
         billed_amount, approved_amount, expected_reimbursement, adjustments_total,
         total_received, reconciled_total, outstanding_balance, variance,
         status, now(), None, "pending", None, now(), now()),
    )

    # Line items
    c.execute("DELETE FROM reconciliation_line_items WHERE reconciliation_id=?", (rec_id,))
    line_items = [
        ("billed", "Total billed amount (claim total_charges)", billed_amount, cld["id"], "claims", cld["id"]),
        ("approved", "Payer-approved amount (claim approved_amount)", approved_amount, cld["id"], "claims", cld["id"]),
        ("expected", "Expected reimbursement (approved − adjustments)", expected_reimbursement, cld["id"], "claims", cld["id"]),
        ("received", "Total payments received (completed)", total_received, cld["id"], "payments", cld["id"]),
        ("reconciled", "Total reconciled payments (verified)", reconciled_total, cld["id"], "payment_matches", cld["id"]),
        ("outstanding", "Outstanding balance (expected − reconciled)", outstanding_balance, cld["id"], "reconciliations", rec_id),
        ("variance", "Reconciliation variance (received − expected)", variance, cld["id"], "reconciliations", rec_id),
    ]
    for line_type, desc, amount, ref_id, src_table, src_id in line_items:
        lid = str(uuid.uuid4())
        c.execute(
            """INSERT INTO reconciliation_line_items(id, reconciliation_id, line_type, description, amount,
                reference_id, source_table, source_id, created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
            (lid, rec_id, line_type, desc, amount, ref_id, src_table, src_id, now()),
        )

    end = datetime.datetime.now(datetime.timezone.utc)
    duration_ms = int((end - start).total_seconds() * 1000)
    agent_execution_complete(c, exec_id, "completed",
                             output_data={"status": status, "variance": variance, "outstanding": outstanding_balance},
                             duration_ms=duration_ms)

    prev_status = c.execute("SELECT status FROM reconciliations WHERE id=?", (rec_id,)).fetchone()
    prev = prev_status["status"] if prev_status else None
    audit(c, actor, "agent", "Reconciliation Agent", "Claim reconciled", "reconciliation", rec_id,
          previous_state=prev, new_state=status,
          input_summary=json.dumps({"billed": billed_amount, "approved": approved_amount, "expected": expected_reimbursement}),
          output_summary=json.dumps({"received": total_received, "reconciled": reconciled_total,
                                     "outstanding": outstanding_balance, "variance": variance}),
          workflow_id=rec_id)

    c.commit()
    row = c.execute("SELECT * FROM reconciliations WHERE id=?", (rec_id,)).fetchone()
    c.close()

    return {
        "reconciliation_id": rec_id,
        "claim_id": cld["id"],
        "claim_number": claim_number,
        "status": status,
        "review_decision": (row["review_decision"] or "pending") if row else "pending",
        "billed_amount": round(billed_amount, 2),
        "approved_amount": round(approved_amount, 2),
        "expected_reimbursement": round(expected_reimbursement, 2),
        "adjustments_total": round(adjustments_total, 2),
        "total_payments_received": round(total_received, 2),
        "reconciled_payments": round(reconciled_total, 2),
        "outstanding_balance": outstanding_balance,
        "variance": variance,
        "currency": CURRENCY,
        "has_incomplete_data": has_incomplete,
        "duplicate_payment_groups": len(duplicate_keys),
        "payment_count": len(received),
        "reconciliation_date": now(),
    }


def reconcile_all_claims(actor: str = "System") -> Dict[str, Any]:
    """Reconcile all claims that have been created."""
    c = get_db()
    claims = c.execute("SELECT id FROM claims").fetchall()
    c.close()
    results = []
    for cl in claims:
        r = reconcile_claim(cl["id"], actor)
        if isinstance(r, tuple):
            results.append({"claim_id": cl["id"], "error": r[0].get("error")})
        else:
            results.append(r)
    return {"reconciled": len(results), "results": results, "currency": CURRENCY}


def get_reconciliation(claim_id: str) -> Optional[Dict[str, Any]]:
    c = get_db()
    row = c.execute("SELECT * FROM reconciliations WHERE claim_id=? OR claim_number=? ORDER BY updated_at DESC LIMIT 1",
                    (claim_id, claim_id)).fetchone()
    if not row:
        c.close(); return None
    result = dict(row)
    result["reconciliation_id"] = result["id"]
    line_items = c.execute(
        "SELECT * FROM reconciliation_line_items WHERE reconciliation_id=? ORDER BY id",
        (result["id"],),
    ).fetchall()
    result["line_items"] = [dict(r) for r in line_items]
    matches = c.execute(
        "SELECT * FROM payment_matches WHERE claim_id=? ORDER BY created_at",
        (row["claim_id"],),
    ).fetchall()
    result["payment_matches"] = [dict(r) for r in matches]
    c.close()
    return result


def list_reconciliations(filters: Dict[str, Any] = None) -> List[dict]:
    c = get_db()
    q = "SELECT * FROM reconciliations WHERE 1=1"
    params: List[Any] = []
    f = filters or {}
    if f.get("status"):
        q += " AND status=?"; params.append(f["status"])
    if f.get("payer"):
        q += " AND (payer_name LIKE ? OR payer_type LIKE ?)"; params.append(f"%{f['payer']}%", f"%{f['payer']}%")
    if f.get("claim_id"):
        q += " AND (claim_id=? OR claim_number=?)"; params.append(f["claim_id"], f["claim_id"])
    q += " ORDER BY updated_at DESC"
    rows = c.execute(q, params).fetchall()
    c.close()
    return [dict(r) for r in rows]


def match_payment(
    claim_id: str,
    payment_id: str,
    matched_amount: float,
    matched_by: str = "User",
    notes: str = "",
) -> Dict[str, Any]:
    """Match a payment to a claim. Creates a payment_match record.

    Does not modify the payment record itself; only creates the match linkage.
    Duplicate matches for the same claim+payment are prevented (idempotent).
    """
    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        c.close(); return {"error": "Claim not found"}, 404
    payment = c.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not payment:
        c.close(); return {"error": "Payment not found"}, 404

    cld = dict(claim)
    payd = dict(payment)

    # Idempotency: existing match for this claim+payment
    existing = c.execute(
        "SELECT id FROM payment_matches WHERE claim_id=? AND payment_id=?",
        (cld["id"], payment_id),
    ).fetchone()
    if existing:
        mid = existing["id"]
        c.execute(
            "UPDATE payment_matches SET matched_amount=?, notes=?, updated_at=? WHERE id=?",
            (_money(matched_amount), notes, now(), mid),
        )
    else:
        mid = str(uuid.uuid4())
        c.execute(
            """INSERT INTO payment_matches(id, claim_id, payment_id, matched_amount, match_status,
                matched_at, matched_by, notes, reconciliation_status, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,'unreconciled',?,?)""",
            (mid, cld["id"], payment_id, _money(matched_amount), "matched", now(), matched_by,
             notes, now(), now()),
        )

    # Duplicate payment alert: same reference + amount on same claim, multiple completed payments
    ref = payd.get("reference") or ""
    amt = _money(payd.get("amount"))
    if ref:
        dups = c.execute(
            """SELECT id FROM payments WHERE claim_id=? AND reference=? AND amount=? AND status IN ('completed','confirmed') AND id<>?""",
            (cld["id"], ref, amt, payment_id),
        ).fetchall()
        if dups:
            add_exception(c, "claims_payment", "high",
                          f"Potential duplicate payment for claim {cld.get('claim_number')}",
                          f"Multiple completed payments share reference '{ref}' and amount {amt:.2f} on claim {cld.get('claim_number')}.",
                          "Investigate and reverse duplicate if confirmed in error.", "Reconciliation Agent",
                          patient_id=cld.get("patient_id"), encounter_id=cld.get("encounter_id"), claim_id=cld["id"])

    audit(c, matched_by, "user", "Reconciliation Agent", "Payment matched to claim", "payment_match", mid,
          previous_state=None, new_state="matched",
          input_summary=json.dumps({"claim_id": cld["id"], "payment_id": payment_id, "matched_amount": matched_amount}),
          output_summary=json.dumps({"match_status": "matched"}))
    c.commit()
    row = c.execute("SELECT * FROM payment_matches WHERE id=?", (mid,)).fetchone()
    c.close()
    return {"match_id": mid, "claim_id": cld["id"], "payment_id": payment_id,
            "matched_amount": round(_money(matched_amount), 2), "match_status": "matched"}


def reconcile_payment_match(match_id: str, decision: str, actor: str = "User", notes: str = "") -> Dict[str, Any]:
    """Human reviewer approves or rejects a payment match reconciliation.

    decision: 'reconciled' or 'rejected'
    """
    if decision not in ("reconciled", "rejected"):
        return {"error": "decision must be 'reconciled' or 'rejected'"}, 400
    c = get_db()
    match = c.execute("SELECT * FROM payment_matches WHERE id=?", (match_id,)).fetchone()
    if not match:
        c.close(); return {"error": "Payment match not found"}, 404
    md = dict(match)
    prev = md.get("reconciliation_status")
    c.execute(
        "UPDATE payment_matches SET reconciliation_status=?, notes=?, updated_at=? WHERE id=?",
        (decision, notes, now(), match_id),
    )
    audit(c, actor, "user", "Reconciliation Agent", f"Payment match {decision}", "payment_match", match_id,
          previous_state=prev, new_state=decision, decision=decision)
    c.commit(); c.close()
    # Re-run reconciliation for the claim to update aggregates (separate connection)
    reconcile_claim(md["claim_id"], actor)
    return {"ok": True, "match_id": match_id, "reconciliation_status": decision}


def detect_underpayments(actor: str = "System") -> Dict[str, Any]:
    """Underpayment Detection Agent.

    Identifies claims where approved/reimbursable amount exceeds reconciled payments.
    Each finding includes evidence from persisted records and a recommended action.
    """
    c = get_db()
    start = datetime.datetime.now(datetime.timezone.utc)
    exec_id = agent_execution_start(c, "Underpayment Detection Agent", "detect_underpayments", "system", None,
                                    {"actor": actor})
    claims = c.execute("SELECT * FROM claims WHERE approved_amount > 0").fetchall()
    created = []
    for cl in claims:
        cld = dict(cl)
        claim_id = cld["id"]
        claim_number = cld.get("claim_number") or claim_id
        payer_name = cld.get("payer_name") or cld.get("payer_type") or "Unknown"

        expected = _money(cld.get("expected_reimbursement"))
        if expected <= 0:
            expected = _money(cld.get("approved_amount"))

        # Reconciled payments for this claim
        matches = [dict(m) for m in c.execute(
            "SELECT * FROM payment_matches WHERE claim_id=? AND reconciliation_status='reconciled'",
            (claim_id,),
        ).fetchall()]
        reconciled_payment = sum(_money(m.get("matched_amount")) for m in matches)

        # All completed payments (received, whether reconciled or not)
        payments = [dict(p) for p in c.execute(
            "SELECT * FROM payments WHERE claim_id=? AND status IN ('completed','confirmed')",
            (claim_id,),
        ).fetchall()]
        total_received = sum(_money(p.get("amount")) for p in payments)

        outstanding = round(expected - reconciled_payment, 2)
        if outstanding <= 0.01:
            continue  # no underpayment

        # Determine reason
        if total_received <= 0.01:
            reason = "approved_but_not_paid"
        elif total_received < expected - 0.01:
            reason = "paid_below_expected"
        else:
            reason = "unexplained_adjustment"

        # Check for existing open underpayment case
        existing = c.execute(
            "SELECT id FROM underpayment_cases WHERE claim_id=? AND status IN ('open','under_review')",
            (claim_id,),
        ).fetchone()
        if existing:
            continue

        case_id = str(uuid.uuid4())
        recommended_action = "Reconcile remittance with payer and follow up on outstanding balance."
        if reason == "approved_but_not_paid":
            recommended_action = "Claim approved but no payment received; initiate payer follow-up."
        elif reason == "paid_below_expected":
            recommended_action = "Partial payment received; request remaining balance from payer."

        c.execute(
            """INSERT INTO underpayment_cases(id, claim_id, claim_number, payer_name, expected_reimbursement,
                reconciled_payment, outstanding_amount, discrepancy_reason, status, recommended_action,
                created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,'open',?,?,?)""",
            (case_id, claim_id, claim_number, payer_name, expected, reconciled_payment,
             outstanding, reason, recommended_action, now(), now()),
        )
        audit(c, actor, "agent", "Underpayment Detection Agent", "Underpayment case created", "underpayment_case", case_id,
              previous_state=None, new_state="open",
              input_summary=json.dumps({"expected": expected, "reconciled": reconciled_payment, "outstanding": outstanding}),
              output_summary=json.dumps({"reason": reason}))
        add_exception(c, "claims_underpayment", "high",
                      f"Underpayment detected: {claim_number}",
                      f"Claim {claim_number} expected {expected:.2f} {CURRENCY}, reconciled {reconciled_payment:.2f} {CURRENCY}, outstanding {outstanding:.2f} {CURRENCY}.",
                      recommended_action, "Underpayment Detection Agent",
                      patient_id=cld.get("patient_id"), encounter_id=cld.get("encounter_id"), claim_id=claim_id)

        created.append({
            "case_id": case_id,
            "claim_id": claim_id,
            "claim_number": claim_number,
            "payer_name": payer_name,
            "expected_reimbursement": round(expected, 2),
            "reconciled_payment": round(reconciled_payment, 2),
            "outstanding_amount": outstanding,
            "discrepancy_reason": reason,
            "status": "open",
        })

    end = datetime.datetime.now(datetime.timezone.utc)
    duration_ms = int((end - start).total_seconds() * 1000)
    agent_execution_complete(c, exec_id, "completed",
                             output_data={"cases_created": len(created)}, duration_ms=duration_ms)
    c.commit(); c.close()
    return {"cases": created, "count": len(created), "currency": CURRENCY}


def list_underpayment_cases(filters: Dict[str, Any] = None) -> List[dict]:
    c = get_db()
    q = "SELECT * FROM underpayment_cases WHERE 1=1"
    params: List[Any] = []
    f = filters or {}
    if f.get("status"):
        q += " AND status=?"; params.append(f["status"])
    if f.get("payer"):
        q += " AND payer_name LIKE ?"; params.append(f"%{f['payer']}%")
    if f.get("claim_id"):
        q += " AND (claim_id=? OR claim_number=?)"; params.append(f["claim_id"], f["claim_id"])
    q += " ORDER BY updated_at DESC"
    rows = c.execute(q, params).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_underpayment_case(case_id: str) -> Optional[Dict[str, Any]]:
    c = get_db()
    row = c.execute("SELECT * FROM underpayment_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        c.close(); return None
    result = dict(row)
    # Include related follow-up and appeal
    if result.get("followup_id"):
        fu = c.execute("SELECT * FROM claim_followups WHERE id=?", (result["followup_id"],)).fetchone()
        result["followup"] = dict(fu) if fu else None
    if result.get("appeal_id"):
        ap = c.execute("SELECT * FROM appeals WHERE id=?", (result["appeal_id"],)).fetchone()
        result["appeal"] = dict(ap) if ap else None
    # Reconciliation detail
    rec = c.execute("SELECT * FROM reconciliations WHERE claim_id=? ORDER BY updated_at DESC LIMIT 1",
                    (result["claim_id"],)).fetchone()
    result["reconciliation"] = dict(rec) if rec else None
    c.close()
    return result


def review_reconciliation(
    rec_id: str,
    decision: str,
    reviewer: str = "User",
    notes: str = "",
    corrected_expected: Optional[float] = None,
) -> Dict[str, Any]:
    """Human reviewer approves, rejects, or requests info on a reconciliation.

    decision: 'approved', 'rejected', 'needs_information'
    """
    if decision not in ("approved", "rejected", "needs_information"):
        return {"error": "decision must be 'approved', 'rejected', or 'needs_information'"}, 400
    c = get_db()
    rec = c.execute("SELECT * FROM reconciliations WHERE id=?", (rec_id,)).fetchone()
    if not rec:
        c.close(); return {"error": "Reconciliation not found"}, 404
    rd = dict(rec)
    prev = rd.get("review_decision")
    new_expected = _money(corrected_expected) if corrected_expected is not None else rd.get("expected_reimbursement", 0)
    c.execute(
        """UPDATE reconciliations SET review_decision=?, reviewed_by=?, review_notes=?,
            expected_reimbursement=?, updated_at=? WHERE id=?""",
        (decision, reviewer, notes, new_expected, now(), rec_id),
    )
    audit(c, reviewer, "user", "Reconciliation Agent", f"Reconciliation {decision}", "reconciliation", rec_id,
          previous_state=prev, new_state=decision, decision=decision,
          input_summary=json.dumps({"corrected_expected": new_expected}),
          output_summary=json.dumps({"review_decision": decision}))
    c.commit(); c.close()
    return {"ok": True, "reconciliation_id": rec_id, "review_decision": decision}


def create_recovery_workflow(
    claim_id: str,
    underpayment_case_id: Optional[str],
    stage: str,
    actor: str = "User",
    notes: str = "",
    financial_value: float = 0.0,
) -> Dict[str, Any]:
    """Record a recovery workflow transition for a claim."""
    allowed_stages = [
        "reconciliation_performed",
        "discrepancy_identified",
        "followup_created",
        "appeal_prepared",
        "human_review",
        "simulated_payer_response",
        "payment_recorded",
        "reconciliation_repeated",
        "recovery_verified",
    ]
    if stage not in allowed_stages:
        return {"error": f"Invalid stage. Allowed: {', '.join(allowed_stages)}"}, 400
    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        c.close(); return {"error": "Claim not found"}, 404
    cld = dict(claim)
    wid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO recovery_workflow(id, claim_id, underpayment_case_id, workflow_stage,
            stage_status, actor, notes, financial_value, created_at, updated_at)
            VALUES(?,?,?,?,'completed',?,?,?,?,?)""",
        (wid, cld["id"], underpayment_case_id, stage, actor, notes, _money(financial_value), now(), now()),
    )
    audit(c, actor, "user", "Reconciliation Agent", f"Recovery workflow: {stage}", "recovery_workflow", wid,
          previous_state=None, new_state="completed",
          input_summary=json.dumps({"stage": stage, "financial_value": financial_value}),
          output_summary=json.dumps({"workflow_stage": stage}))
    c.commit(); c.close()
    return {"workflow_id": wid, "claim_id": cld["id"], "stage": stage, "stage_status": "completed"}


def list_recovery_workflow(claim_id: str) -> List[dict]:
    c = get_db()
    claim = c.execute("SELECT id FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        c.close(); return []
    rows = c.execute(
        "SELECT * FROM recovery_workflow WHERE claim_id=? ORDER BY created_at",
        (claim["id"],),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def record_simulated_payer_response(
    claim_id: str,
    response_type: str,
    amount: float = 0.0,
    actor: str = "User",
    notes: str = "",
) -> Dict[str, Any]:
    """Record a simulated payer response. Clearly marked as simulated.

    response_type: 'payment_approved', 'payment_partially_approved', 'additional_documentation_requested',
                   'appeal_accepted', 'appeal_rejected', 'payment_issued', 'payment_delayed'
    """
    allowed = [
        "payment_approved", "payment_partially_approved", "additional_documentation_requested",
        "appeal_accepted", "appeal_rejected", "payment_issued", "payment_delayed",
    ]
    if response_type not in allowed:
        return {"error": f"Invalid response_type. Allowed: {', '.join(allowed)}"}, 400
    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        c.close(); return {"error": "Claim not found"}, 404
    cld = dict(claim)

    # Create an exception to record the simulated response for traceability
    xid = add_exception(c, "payer_response_simulated", "medium",
                        f"Simulated payer response: {response_type}",
                        f"SIMULATED — Claim {cld.get('claim_number')}: payer response '{response_type}' amount {_money(amount):.2f} {CURRENCY}. This is a demo simulation, not an actual payer communication.",
                        "Review and record actual payment if issued.", "Simulated Payer Response",
                        patient_id=cld.get("patient_id"), encounter_id=cld.get("encounter_id"), claim_id=cld["id"])

    # Update claim status based on response
    new_status = cld["status"]
    if response_type == "payment_approved":
        new_status = "approved"
    elif response_type == "payment_partially_approved":
        new_status = "partially_approved"
    elif response_type == "payment_issued":
        new_status = "paid"
    elif response_type == "appeal_accepted":
        new_status = "under_review"
    elif response_type == "appeal_rejected":
        new_status = "denied"
    elif response_type == "payment_delayed":
        new_status = "awaiting_payment"

    c.execute("UPDATE claims SET status=?, updated_at=? WHERE id=?", (new_status, now(), cld["id"]))
    audit(c, actor, "user", "Simulated Payer Response", f"Simulated payer response: {response_type}", "claim", cld["id"],
          previous_state=cld["status"], new_state=new_status,
          input_summary=json.dumps({"response_type": response_type, "amount": amount}),
          output_summary=json.dumps({"new_status": new_status}))
    c.commit(); c.close()
    return {
        "ok": True,
        "simulated": True,
        "response_type": response_type,
        "amount": round(_money(amount), 2),
        "currency": CURRENCY,
        "claim_id": cld["id"],
        "claim_number": cld.get("claim_number"),
        "new_status": new_status,
        "audit_event_id": xid,
        "notice": "SIMULATED payer response — not an actual payer communication.",
    }


def record_simulated_payment(
    claim_id: str,
    amount: float,
    method: str,
    reference: str,
    actor: str = "User",
    payment_status: str = "completed",
) -> Dict[str, Any]:
    """Record a simulated payment against a claim.

    Creates a payment record linked to the claim. Payment must be reconciled
    before it counts as verified recovery.
    """
    allowed_methods = {"Cash", "Mobile Money", "Card", "Bank Transfer"}
    if method not in allowed_methods:
        return {"error": f"payment_method must be one of {sorted(allowed_methods)}"}, 400
    if _money(amount) <= 0:
        return {"error": "amount must be positive"}, 400
    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        c.close(); return {"error": "Claim not found"}, 404
    cld = dict(claim)

    pay_id = str(uuid.uuid4())
    c.execute(
        """INSERT INTO payments(id, claim_id, bill_id, patient_id, amount, payment_method,
            payment_date, status, reference, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (pay_id, cld["id"], cld.get("bill_id"), cld.get("patient_id"), _money(amount),
         method, now(), payment_status, reference, now()),
    )
    audit(c, actor, "user", "Reconciliation Agent", f"Simulated payment recorded: {amount:.2f} {CURRENCY}", "payment", pay_id,
          previous_state=None, new_state=payment_status,
          input_summary=json.dumps({"amount": amount, "method": method, "reference": reference}),
          output_summary=json.dumps({"payment_id": pay_id}))
    c.commit()
    row = c.execute("SELECT * FROM payments WHERE id=?", (pay_id,)).fetchone()
    c.close()
    return {
        "payment_id": pay_id,
        "claim_id": cld["id"],
        "amount": round(_money(amount), 2),
        "payment_method": method,
        "reference": reference,
        "status": payment_status,
        "currency": CURRENCY,
        "notice": "Payment recorded. Must be matched and reconciled before counting as verified recovery.",
    }


def reconciliation_metrics() -> Dict[str, Any]:
    """DB-derived reconciliation and recovery metrics."""
    c = get_db()
    recs = [dict(r) for r in c.execute("SELECT * FROM reconciliations").fetchall()]
    matches = [dict(r) for r in c.execute("SELECT * FROM payment_matches WHERE reconciliation_status='reconciled'").fetchall()]
    cases = [dict(r) for r in c.execute("SELECT * FROM underpayment_cases").fetchall()]
    recoveries = [dict(r) for r in c.execute(
        "SELECT * FROM recovery_workflow WHERE workflow_stage='recovery_verified'",
    ).fetchall()]

    total_expected = sum(_money(r.get("expected_reimbursement")) for r in recs)
    total_received = sum(_money(r.get("total_payments_received")) for r in recs)
    total_reconciled = sum(_money(r.get("reconciled_payments")) for r in recs)
    total_outstanding = sum(_money(r.get("outstanding_balance")) for r in recs)
    total_variance = sum(_money(r.get("variance")) for r in recs)
    verified_recovered = sum(_money(r.get("reconciled_payments")) for r in recs if (r.get("review_decision") or "").lower() == "approved")

    underpaid_count = sum(1 for r in recs if (r.get("status") or "") == "Underpaid")
    partially_paid_count = sum(1 for r in recs if (r.get("status") or "") == "Partially Paid")
    underpaid_claims = underpaid_count + partially_paid_count
    unreconciled_payments = sum(1 for m in matches if (m.get("reconciliation_status") or "") != "reconciled")

    reconciled_count = sum(1 for r in recs if (r.get("status") or "") == "Reconciled")
    total_reconcilable = sum(1 for r in recs if (r.get("status") or "") not in ("Awaiting Adjudication", "Awaiting Payment"))
    completion_rate = (reconciled_count / total_reconcilable * 100) if total_reconcilable else 0

    open_cases = sum(1 for c_ in cases if (c_.get("status") or "") in ("open", "under_review"))
    resolved_cases = sum(1 for c_ in cases if (c_.get("status") or "") == "resolved")
    recovery_success_rate = (resolved_cases / len(cases) * 100) if cases else 0

    # Average time to reconciliation (from claim created_at to reconciliation created_at)
    times = []
    for r in recs:
        claim = c.execute("SELECT created_at FROM claims WHERE id=?", (r["claim_id"],)).fetchone()
        if claim and r.get("created_at"):
            try:
                d1 = datetime.datetime.fromisoformat(claim["created_at"][:19])
                d2 = datetime.datetime.fromisoformat(r["created_at"][:19])
                times.append((d2 - d1).total_seconds())
            except Exception:
                pass
    avg_time_to_recon = round(sum(times) / len(times), 2) if times else 0

    # Average time to recovery (from case created to resolved)
    rec_times = []
    for case_ in cases:
        if case_.get("resolved_at") and case_.get("created_at"):
            try:
                d1 = datetime.datetime.fromisoformat(case_["created_at"][:19])
                d2 = datetime.datetime.fromisoformat(case_["resolved_at"][:19])
                rec_times.append((d2 - d1).total_seconds())
            except Exception:
                pass
    avg_time_to_recovery = round(sum(rec_times) / len(rec_times), 2) if rec_times else 0

    # Unresolved discrepancy aging
    aging_buckets = {"0-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    today = datetime.date.today()
    for case_ in cases:
        if (case_.get("status") or "") not in ("resolved", "closed"):
            ref = (case_.get("created_at") or "")[:10]
            try:
                d = datetime.date.fromisoformat(ref)
                age = (today - d).days
                if age <= 30: aging_buckets["0-30"] += 1
                elif age <= 60: aging_buckets["31-60"] += 1
                elif age <= 90: aging_buckets["61-90"] += 1
                else: aging_buckets["90+"] += 1
            except Exception:
                pass

    c.close()
    return {
        "currency": CURRENCY,
        "kpis": {
            "total_expected_reimbursement": {"label": "Total expected reimbursement", "value": round(total_expected, 2),
                "definition": "Sum of expected reimbursement across all reconciled claims", "method": "SUM(reconciliations.expected_reimbursement)"},
            "total_reconciled_payments": {"label": "Total reconciled payments", "value": round(total_reconciled, 2),
                "definition": "Sum of payments matched and verified as reconciled", "method": "SUM(reconciliations.reconciled_payments)"},
            "total_outstanding_reimbursement": {"label": "Total outstanding reimbursement", "value": round(total_outstanding, 2),
                "definition": "Expected reimbursement minus reconciled payments", "method": "SUM(reconciliations.outstanding_balance)"},
            "total_underpaid_claims": {"label": "Total underpaid claims", "value": underpaid_claims,
                "definition": "Claims with status Underpaid or Partially Paid", "method": "COUNT where status in Underpaid,Partially Paid"},
            "total_unreconciled_payments": {"label": "Total unreconciled payments", "value": unreconciled_payments,
                "definition": "Payment matches not yet reconciled", "method": "COUNT payment_matches where reconciliation_status != reconciled"},
            "verified_recovered_revenue": {"label": "Verified recovered revenue", "value": round(verified_recovered, 2),
                "definition": "Reconciled payments with human-approved reconciliation", "method": "SUM reconciled_payments where review_decision = approved"},
            "reconciliation_completion_rate": {"label": "Reconciliation completion rate", "value": round(completion_rate, 1),
                "unit": "%", "definition": "Reconciled claims over reconcilable claims", "method": "reconciled/total_reconcilable*100"},
        },
        "counts": {
            "total_reconciliations": len(recs),
            "reconciled": reconciled_count,
            "underpaid": underpaid_count,
            "partially_paid": partially_paid_count,
            "open_underpayment_cases": open_cases,
            "resolved_underpayment_cases": resolved_cases,
            "verified_recoveries": len(recoveries),
        },
        "rates": {
            "reconciliation_completion_rate": round(completion_rate, 1),
            "recovery_success_rate": round(recovery_success_rate, 1),
        },
        "timing": {
            "average_time_to_reconciliation_seconds": avg_time_to_recon,
            "average_time_to_recovery_seconds": avg_time_to_recovery,
        },
        "aging": aging_buckets,
    }


def export_reconciliation_csv(columns: List[str] = None) -> str:
    """Export reconciliation records to CSV."""
    import csv, io
    c = get_db()
    rows = [dict(r) for r in c.execute(
        """SELECT r.claim_number, r.payer_name, r.billed_amount, r.approved_amount, r.expected_reimbursement,
                  r.total_payments_received, r.reconciled_payments, r.outstanding_balance, r.variance,
                  r.status, r.review_decision, r.updated_at
           FROM reconciliations r ORDER BY r.updated_at DESC""",
    ).fetchall()]
    c.close()
    if columns is None:
        columns = ["claim_number", "payer_name", "billed_amount", "approved_amount", "expected_reimbursement",
                   "total_payments_received", "reconciled_payments", "outstanding_balance", "variance",
                   "status", "review_decision", "updated_at"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r[k] for k in columns if k in r})
    return buf.getvalue()
