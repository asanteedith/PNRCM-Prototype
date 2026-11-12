"""Revenue Integrity Agent (deterministic, explainable)."""
from __future__ import annotations
import json, uuid
from typing import Any, Dict, List
from db import get_db, now, audit

def _risk_level(score: int) -> str:
    if score >= 80: return "CRITICAL"
    if score >= 60: return "HIGH"
    if score >= 30: return "MEDIUM"
    return "LOW"

def _upsert_alert(c, sig: str, alert: Dict[str, Any]) -> str:
    # avoid duplicate unresolved alerts for same signature
    existing = c.execute(
        "SELECT id FROM fraud_alerts WHERE detection_rule=? AND status NOT IN ('RESOLVED','FALSE_POSITIVE','INCONCLUSIVE') AND (related_claim_id IS ? OR related_claim_id=?) AND (related_payment_id IS ? OR related_payment_id=?) LIMIT 1",
        (sig, alert.get("related_claim_id"), alert.get("related_claim_id"), alert.get("related_payment_id"), alert.get("related_payment_id"))
    ).fetchone()
    if existing:
        return existing["id"]
    aid = str(uuid.uuid4())
    ts = now()
    c.execute(
        """INSERT INTO fraud_alerts(id,detection_date,risk_score,risk_level,anomaly_category,related_claim_id,related_encounter_id,related_bill_id,related_payment_id,provider_or_department,estimated_exposure,detection_rule,evidence,explanation,recommended_action,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'NEW',?,?)""",
        (aid, ts, alert["risk_score"], alert["risk_level"], alert["anomaly_category"],
         alert.get("related_claim_id"), alert.get("related_encounter_id"), alert.get("related_bill_id"), alert.get("related_payment_id"),
         alert.get("provider_or_department"), alert.get("estimated_exposure",0), sig,
         json.dumps(alert.get("evidence",{})), alert.get("explanation",""), alert.get("recommended_action",""), ts, ts)
    )
    # create investigation shell
    iid = str(uuid.uuid4())
    c.execute("INSERT INTO fraud_investigations(id,alert_id,status,created_at,updated_at) VALUES(?,?,'NEW',?,?)", (iid, aid, ts, ts))
    audit(c, "Revenue Integrity Agent", "agent", "Financial Integrity", f"Alert created: {alert['anomaly_category']}", "fraud_alert", aid, new_state="NEW")
    return aid

def run_detection() -> Dict[str, Any]:
    c = get_db()
    created = 0

    # Duplicate claims (same encounter + payer + amount)
    claims = c.execute("SELECT * FROM claims").fetchall()
    seen = {}
    for cl in claims:
        key = (cl["encounter_id"], cl["payer_type"], round(float(cl["total_charges"] or 0),2))
        seen.setdefault(key, []).append(dict(cl))
    for key, group in seen.items():
        if len(group) > 1:
            score = 75
            aid = _upsert_alert(c, "DUPLICATE_CLAIM", {
                "risk_score": score, "risk_level": _risk_level(score), "anomaly_category": "Duplicate claims",
                "related_claim_id": group[0]["id"], "related_encounter_id": group[0]["encounter_id"],
                "estimated_exposure": float(group[1].get("total_charges") or 0),
                "evidence": {"claim_ids": [g["id"] for g in group], "claim_numbers": [g.get("claim_number") for g in group]},
                "explanation": "Multiple claims share the same encounter, payer, and amount in synthetic records.",
                "recommended_action": "Review for duplicate submission; retain one valid claim record."
            })
            created += 1

    # Billing without corresponding encounter
    bills = c.execute("SELECT b.*, e.id as enc_exists FROM bills b LEFT JOIN encounters e ON b.encounter_id=e.id").fetchall()
    for b in bills:
        if not b["enc_exists"]:
            score = 85
            _upsert_alert(c, "BILL_NO_ENCOUNTER:"+b["id"], {
                "risk_score": score, "risk_level": _risk_level(score), "anomaly_category": "Billing without corresponding encounter",
                "related_bill_id": b["id"], "estimated_exposure": float(b["total"] or 0),
                "evidence": {"bill_id": b["id"], "encounter_id": b["encounter_id"]},
                "explanation": "A bill references an encounter that is not present in records.",
                "recommended_action": "Verify encounter linkage and supporting documentation."
            }); created += 1

    # Duplicate payments (same reference and amount and bill)
    pays = c.execute("SELECT * FROM patient_payments WHERE payment_status='confirmed'").fetchall()
    pseen = {}
    for p in pays:
        pd = dict(p)
        key = (pd.get("reference"), float(pd.get("amount") or 0), pd.get("balance_id"))
        if key[0]:
            pseen.setdefault(key, []).append(pd)
    for key, group in pseen.items():
        if len(group) > 1:
            score = 70
            _upsert_alert(c, "DUPLICATE_PAYMENT:"+str(key[0]), {
                "risk_score": score, "risk_level": _risk_level(score), "anomaly_category": "Duplicate payments",
                "related_payment_id": group[0]["id"], "estimated_exposure": float(group[1].get("amount") or 0),
                "evidence": {"payment_ids": [g["id"] for g in group], "reference": key[0]},
                "explanation": "Multiple confirmed payments share the same reference, amount, and balance.",
                "recommended_action": "Verify transaction; reverse duplicate if confirmed in error."
            }); created += 1

    # Suspicious reversals
    reversals = c.execute("SELECT * FROM patient_payments WHERE payment_status='reversed'").fetchall()
    for r in reversals:
        rd = dict(r)
        score = 45
        _upsert_alert(c, "REVERSAL:"+r["id"], {
            "risk_score": score, "risk_level": _risk_level(score), "anomaly_category": "Suspicious payment reversals",
            "related_payment_id": r["id"], "estimated_exposure": float(rd.get("amount") or 0),
            "evidence": {"payment_id": r["id"]},
            "explanation": "A payment reversal was recorded; review for legitimacy.",
            "recommended_action": "Confirm reversal authorization and reconcile balance."
        }); created += 1

    # Unreconciled discrepancies (bill outstanding large / claim underpaid)
    underpaid = c.execute("SELECT * FROM claims WHERE status='paid' AND approved_amount>0 AND paid_amount < approved_amount-0.01").fetchall()
    for cl in underpaid:
        diff = float(cl["approved_amount"] or 0) - float(cl["paid_amount"] or 0)
        score = 55
        _upsert_alert(c, "UNDERPAYMENT:"+cl["id"], {
            "risk_score": score, "risk_level": _risk_level(score), "anomaly_category": "Unreconciled financial discrepancies",
            "related_claim_id": cl["id"], "related_encounter_id": cl["encounter_id"],
            "estimated_exposure": diff,
            "evidence": {"approved": cl["approved_amount"], "paid": cl["paid_amount"]},
            "explanation": "Approved amount exceeds recorded payment; possible underpayment or reconciliation gap.",
            "recommended_action": "Reconcile with payer remittance and follow up."
        }); created += 1

    # Unusual billing amounts (simple z-like heuristic: > 3x average)
    rows = c.execute("SELECT AVG(total) avg_total FROM bills WHERE total>0").fetchone()
    avg = float(rows["avg_total"] or 0)
    if avg > 0:
        big = c.execute("SELECT * FROM bills WHERE total > ?", (avg * 3,)).fetchall()
        for b in big:
            score = 50
            _upsert_alert(c, "UNUSUAL_AMOUNT:"+b["id"], {
                "risk_score": score, "risk_level": _risk_level(score), "anomaly_category": "Unusual billing amounts",
                "related_bill_id": b["id"], "related_encounter_id": b["encounter_id"],
                "estimated_exposure": float(b["total"] or 0),
                "evidence": {"bill_total": b["total"], "average": round(avg,2)},
                "explanation": "Bill total exceeds 3x the current average in synthetic records (heuristic).",
                "recommended_action": "Review itemization and supporting documentation."
            }); created += 1

    c.commit(); c.close()
    return {"scanned": True, "alerts_considered": created, "note": "Deterministic, explainable simulated rules; not proof of irregularity."}

def list_alerts(status: str = None) -> List[dict]:
    c = get_db()
    if status:
        rows = c.execute("SELECT * FROM fraud_alerts WHERE status=? ORDER BY risk_score DESC, created_at DESC", (status,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM fraud_alerts ORDER BY risk_score DESC, created_at DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]

def get_alert(alert_id: str):
    c = get_db()
    r = c.execute("SELECT * FROM fraud_alerts WHERE id=?", (alert_id,)).fetchone()
    inv = c.execute("SELECT * FROM fraud_investigations WHERE alert_id=? ORDER BY created_at DESC LIMIT 1", (alert_id,)).fetchone()
    c.close()
    return {"alert": dict(r) if r else None, "investigation": dict(inv) if inv else None}

def update_investigation(alert_id: str, updates: dict, actor: str = "User") -> Dict[str, Any]:
    c = get_db()
    inv = c.execute("SELECT * FROM fraud_investigations WHERE alert_id=? ORDER BY created_at DESC LIMIT 1", (alert_id,)).fetchone()
    if not inv:
        iid = str(uuid.uuid4())
        c.execute("INSERT INTO fraud_investigations(id,alert_id,status,created_at,updated_at) VALUES(?,?,'NEW',?,?)", (iid, alert_id, now(), now()))
        inv = c.execute("SELECT * FROM fraud_investigations WHERE id=?", (iid,)).fetchone()
    allowed_i = {"status","assigned_investigator","notes","human_decision","confirmed_discrepancy","amount_corrected","amount_recovered"}
    sets, vals = [], []
    for k,v in updates.items():
        if k in allowed_i:
            sets.append(f"{k}=?"); vals.append(v)
    if "status" in updates:
        resolved = now() if updates["status"] in ("RESOLVED","FALSE_POSITIVE","INCONCLUSIVE","ESCALATED") else None
        sets.append("resolved_at=?"); vals.append(resolved)
        c.execute("UPDATE fraud_alerts SET status=?, updated_at=?, resolved_at=? WHERE id=?", (updates["status"], now(), resolved, alert_id))
    if sets:
        sets.append("updated_at=?"); vals.append(now()); vals.append(inv["id"])
        c.execute(f"UPDATE fraud_investigations SET {', '.join(sets)} WHERE id=?", vals)
    audit(c, actor, "user", "Financial Integrity", "Investigation updated", "fraud_alert", alert_id, new_state=updates.get("status"))
    c.commit(); c.close()
    return {"ok": True}

def integrity_metrics() -> Dict[str, Any]:
    c = get_db()
    alerts = c.execute("SELECT * FROM fraud_alerts").fetchall()
    total = len(alerts)
    critical = sum(1 for a in alerts if a["risk_level"] == "CRITICAL")
    high = sum(1 for a in alerts if a["risk_level"] == "HIGH")
    open_inv = sum(1 for a in alerts if a["status"] in ("NEW","UNDER_REVIEW","INVESTIGATION_IN_PROGRESS","ESCALATED"))
    exposure = sum(float(a["estimated_exposure"] or 0) for a in alerts if a["status"] not in ("FALSE_POSITIVE","RESOLVED"))
    invs = c.execute("SELECT * FROM fraud_investigations").fetchall()
    confirmed = sum(float(i["confirmed_discrepancy"] or 0) for i in invs)
    corrected = sum(float(i["amount_corrected"] or 0) for i in invs)
    recovered = sum(float(i["amount_recovered"] or 0) for i in invs)
    fp = sum(1 for a in alerts if a["status"] == "FALSE_POSITIVE")
    fp_rate = (fp/total*100) if total else 0
    c.close()
    return {
        "total_alerts": total, "critical_alerts": critical, "high_risk_alerts": high,
        "open_investigations": open_inv, "estimated_exposure": round(exposure,2),
        "confirmed_discrepancies": round(confirmed,2), "amount_corrected": round(corrected,2),
        "amount_recovered": round(recovered,2), "false_positive_rate": round(fp_rate,1)
    }
