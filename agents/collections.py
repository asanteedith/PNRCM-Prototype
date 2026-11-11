"""Patient copayment, balances, collections, and payment recording (simulated)."""
from __future__ import annotations
import json, uuid, datetime
from typing import Any, Dict, List
from db import get_db, now, audit, add_exception

def get_copay_amount(payer_type: str, encounter_type: str) -> float:
    c = get_db()
    r = c.execute(
        "SELECT amount FROM copayment_rules WHERE is_active=1 AND payer_type=? AND encounter_type=? ORDER BY created_at DESC LIMIT 1",
        (payer_type or "self_pay", encounter_type or "outpatient")
    ).fetchone()
    if not r:
        r = c.execute("SELECT amount FROM copayment_rules WHERE is_active=1 AND payer_type='self_pay' LIMIT 1").fetchone()
    c.close()
    return float(r["amount"]) if r else 0.0

def ensure_balance(patient_id: str, encounter_id: str, payer_type: str = "self_pay", encounter_type: str = "outpatient") -> Dict[str, Any]:
    c = get_db()
    pat = c.execute("SELECT id, patient_id FROM patients WHERE id=? OR patient_id=?", (patient_id, patient_id)).fetchone()
    enc = c.execute("SELECT id, encounter_id FROM encounters WHERE id=? OR encounter_id=?", (encounter_id, encounter_id)).fetchone()
    if not pat or not enc:
        c.close(); return {"error": "Patient or encounter not found"}
    existing = c.execute("SELECT * FROM patient_balances WHERE patient_id=? AND encounter_id=?", (pat["id"], enc["id"])).fetchone()
    if existing:
        c.close(); return dict(existing)
    expected = get_copay_amount(payer_type, encounter_type)
    bid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO patient_balances(id,patient_id,encounter_id,expected_copayment,amount_due,amount_paid,outstanding,status,created_at,updated_at)
           VALUES(?,?,?,?,?,0,?,'COPAYMENT_PENDING',?,?)""",
        (bid, pat["id"], enc["id"], expected, expected, expected, now(), now())
    )
    audit(c, "Patient Collections Agent", "agent", "Patient Collections", "Copayment/balance created", "patient_balance", bid, new_state="COPAYMENT_PENDING")
    c.commit()
    row = c.execute("SELECT * FROM patient_balances WHERE id=?", (bid,)).fetchone()
    c.close()
    return dict(row)

def list_balances(filters: dict = None) -> List[dict]:
    c = get_db()
    rows = c.execute(
        """SELECT b.*, p.patient_id as mrn, p.first_name, p.last_name, e.encounter_id as enc_no
           FROM patient_balances b
           LEFT JOIN patients p ON b.patient_id=p.id
           LEFT JOIN encounters e ON b.encounter_id=e.id
           ORDER BY b.updated_at DESC"""
    ).fetchall()
    c.close()
    items = [dict(r) for r in rows]
    f = filters or {}
    if f.get("status"): items = [x for x in items if x["status"] == f["status"]]
    if f.get("patient"): items = [x for x in items if f["patient"] in (x.get("patient_id",""), x.get("mrn",""))]
    return items

def record_payment(balance_id: str, amount: float, method: str, reference: str = "", recorded_by: str = "User", payment_status: str = "confirmed") -> Dict[str, Any]:
    if amount <= 0:
        return {"error": "amount must be positive"}
    allowed_methods = {"Cash","Mobile Money","Card","Bank Transfer"}
    if method not in allowed_methods:
        return {"error": f"payment_method must be one of {sorted(allowed_methods)}"}
    c = get_db()
    bal = c.execute("SELECT * FROM patient_balances WHERE id=?", (balance_id,)).fetchone()
    if not bal:
        c.close(); return {"error": "Balance not found"}
    bald = dict(bal)
    pid = str(uuid.uuid4())
    # only confirmed payments affect balance
    effective = amount if payment_status == "confirmed" else 0.0
    new_paid = float(bald.get("amount_paid") or 0) + effective
    new_out = max(float(bald.get("amount_due") or 0) - new_paid, 0.0)
    if payment_status == "confirmed":
        if new_out <= 0.01:
            status = "PAID"
            new_out = 0.0
        elif new_paid > 0:
            status = "PARTIALLY_PAID"
        else:
            status = bald["status"]
    else:
        status = bald["status"]
    c.execute(
        """INSERT INTO patient_payments(id,balance_id,patient_id,encounter_id,amount,payment_method,payment_date,payment_status,reference,recorded_by,reconciliation_status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,'unreconciled',?)""",
        (pid, balance_id, bald["patient_id"], bald["encounter_id"], amount, method, now(), payment_status, reference, recorded_by, now())
    )
    if payment_status == "confirmed":
        c.execute("UPDATE patient_balances SET amount_paid=?, outstanding=?, status=?, updated_at=? WHERE id=?",
                  (new_paid, new_out, status, now(), balance_id))
        # Also update bills.outstanding for bills linked to this encounter
        try:
            enc_id = bald.get("encounter_id")
            if enc_id:
                bill_rows = c.execute("SELECT id, outstanding, total FROM bills WHERE encounter_id=? AND status NOT IN ('paid','rejected')", (enc_id,)).fetchall()
                for brow in bill_rows:
                    bd = dict(brow)
                    new_bill_out = max(float(bd.get("outstanding") or 0) - effective, 0.0)
                    new_bill_status = "paid" if new_bill_out <= 0.01 else bd.get("status", "draft")
                    c.execute("UPDATE bills SET outstanding=?, status=?, updated_at=? WHERE id=?", (new_bill_out, new_bill_status, now(), bd["id"]))
        except Exception:
            pass
    audit(c, recorded_by, "user", "Patient Collections", f"Patient payment {payment_status}", "patient_payment", pid,
          previous_state=bald["status"], new_state=status)
    c.commit()
    row = c.execute("SELECT * FROM patient_payments WHERE id=?", (pid,)).fetchone()
    bal2 = c.execute("SELECT * FROM patient_balances WHERE id=?", (balance_id,)).fetchone()
    c.close()
    return {"payment": dict(row), "balance": dict(bal2) if bal2 else None}

def reverse_payment(payment_id: str, recorded_by: str = "User", reason: str = "") -> Dict[str, Any]:
    c = get_db()
    pay = c.execute("SELECT * FROM patient_payments WHERE id=?", (payment_id,)).fetchone()
    if not pay:
        c.close(); return {"error": "Payment not found"}
    if pay["payment_status"] == "reversed":
        c.close(); return {"error": "Payment already reversed"}
    bal = c.execute("SELECT * FROM patient_balances WHERE id=?", (pay["balance_id"],)).fetchone()
    rid = str(uuid.uuid4())
    # create reversal record
    c.execute(
        """INSERT INTO patient_payments(id,balance_id,patient_id,encounter_id,amount,payment_method,payment_date,payment_status,reference,recorded_by,reconciliation_status,reversal_of,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,'unreconciled',?,?)""",
        (rid, pay["balance_id"], pay["patient_id"], pay["encounter_id"], -float(pay["amount"]), pay["payment_method"], now(), "confirmed", f"REVERSAL:{pay['id']}", recorded_by, pay["id"], now())
    )
    c.execute("UPDATE patient_payments SET payment_status='reversed' WHERE id=?", (payment_id,))
    if bal and pay["payment_status"] == "confirmed":
        new_paid = max(float(bal["amount_paid"] or 0) - float(pay["amount"]), 0.0)
        new_out = max(float(bal["amount_due"] or 0) - new_paid, 0.0)
        status = "PAYMENT_DUE" if new_out > 0.01 else "PAID"
        c.execute("UPDATE patient_balances SET amount_paid=?, outstanding=?, status=?, updated_at=? WHERE id=?",
                  (new_paid, new_out, status, now(), bal["id"]))
    audit(c, recorded_by, "user", "Patient Collections", "Payment reversed", "patient_payment", payment_id, previous_state="confirmed", new_state="reversed", decision=reason)
    c.commit(); c.close()
    return {"ok": True, "reversal_id": rid}

def list_patient_payments(balance_id: str = None) -> List[dict]:
    c = get_db()
    if balance_id:
        rows = c.execute("SELECT * FROM patient_payments WHERE balance_id=? ORDER BY created_at DESC", (balance_id,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM patient_payments ORDER BY created_at DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]

def create_collection_task(balance_id: str, priority: str = "medium", assigned_staff: str = "Unassigned", note: str = "") -> Dict[str, Any]:
    c = get_db()
    bal = c.execute("SELECT * FROM patient_balances WHERE id=?", (balance_id,)).fetchone()
    if not bal:
        c.close(); return {"error": "Balance not found"}
    tid = str(uuid.uuid4())
    due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)).date().isoformat()
    c.execute(
        "INSERT INTO collection_tasks(id,balance_id,patient_id,encounter_id,status,priority,assigned_staff,due_date,note,created_at,updated_at) VALUES(?,?,?,?, 'open',?,?,?,?,?,?)",
        (tid, balance_id, bal["patient_id"], bal["encounter_id"], priority, assigned_staff, due, note, now(), now())
    )
    c.execute("UPDATE patient_balances SET status='COLLECTION_FOLLOW_UP', updated_at=? WHERE id=? AND status NOT IN ('PAID','WAIVED','DISPUTED')", (now(), balance_id))
    audit(c, "Patient Collections Agent", "agent", "Patient Collections", "Collection task created", "collection_task", tid, new_state="open")
    c.commit(); c.close()
    return {"task_id": tid}

def list_collection_tasks() -> List[dict]:
    c = get_db()
    rows = c.execute("SELECT t.*, p.patient_id as mrn, p.first_name, p.last_name FROM collection_tasks t LEFT JOIN patients p ON t.patient_id=p.id ORDER BY t.updated_at DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]

def add_collection_attempt(task_id: str, channel: str, template: str, outcome: str, note: str = "") -> Dict[str, Any]:
    c = get_db()
    t = c.execute("SELECT * FROM collection_tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        c.close(); return {"error": "Task not found"}
    aid = str(uuid.uuid4())
    c.execute("INSERT INTO collection_attempts(id,task_id,ts,channel,template,outcome,note) VALUES(?,?,?,?,?,?,?)",
              (aid, task_id, now(), channel, template, outcome, note))
    c.commit(); c.close()
    return {"attempt_id": aid, "simulated": True}

def set_balance_status(balance_id: str, status: str, actor: str = "User", note: str = "") -> Dict[str, Any]:
    allowed = {"COPAYMENT_PENDING","PAYMENT_DUE","PARTIALLY_PAID","PAID","COLLECTION_FOLLOW_UP","DISPUTED","WAIVED","CLOSED"}
    if status not in allowed:
        return {"error": "Invalid status"}
    c = get_db()
    r = c.execute("SELECT * FROM patient_balances WHERE id=?", (balance_id,)).fetchone()
    if not r:
        c.close(); return {"error": "Balance not found"}
    c.execute("UPDATE patient_balances SET status=?, updated_at=? WHERE id=?", (status, now(), balance_id))
    audit(c, actor, "user", "Patient Collections", f"Balance status set to {status}", "patient_balance", balance_id, previous_state=r["status"], new_state=status, decision=note)
    c.commit(); c.close()
    return {"ok": True, "status": status}

def collections_metrics() -> Dict[str, Any]:
    c = get_db()
    bals = c.execute("SELECT * FROM patient_balances").fetchall()
    total_due = sum(float(r["expected_copayment"] or 0) for r in bals)
    collected = sum(float(r["amount_paid"] or 0) for r in bals)
    outstanding = sum(float(r["outstanding"] or 0) for r in bals)
    partial = sum(1 for r in bals if r["status"] == "PARTIALLY_PAID")
    overdue = sum(1 for r in bals if r["status"] in ("PAYMENT_DUE","COLLECTION_FOLLOW_UP") and float(r["outstanding"] or 0) > 0)
    tasks = c.execute("SELECT COUNT(*) n FROM collection_tasks WHERE status='open'").fetchone()["n"]
    c.close()
    rate = (collected/total_due*100) if total_due > 0 else 0
    return {
        "total_copayments_due": round(total_due,2), "total_collected": round(collected,2),
        "outstanding": round(outstanding,2), "partially_paid_accounts": partial,
        "overdue_balances": overdue, "collection_rate": round(rate,1), "open_followup_tasks": tasks
    }

def communication_template(kind: str, name: str = "Patient", amount: float = 0.0, due_date: str = "") -> Dict[str, str]:
    templates = {
        "copayment_due": f"Dear {name}, a copayment of GHS {amount:.2f} is due for your recent visit. If you need assistance or wish to discuss this, please contact our billing office. (Simulated message)",
        "outstanding_balance": f"Dear {name}, our records show an outstanding balance of GHS {amount:.2f}. Please contact us to arrange payment or discuss assistance options. (Simulated message)",
        "payment_promise": f"Dear {name}, this is a reminder of your payment arrangement of GHS {amount:.2f} due {due_date}. Contact us if you need to reschedule. (Simulated message)",
        "overdue_payment": f"Dear {name}, your balance of GHS {amount:.2f} is past due. Please reach out to settle or discuss a plan. We can assist with questions or disputes. (Simulated message)",
        "payment_confirmation": f"Dear {name}, we received your payment of GHS {amount:.2f}. Thank you. If you have questions, please contact our billing office. (Simulated message)",
    }
    body = templates.get(kind, templates["outstanding_balance"])
    return {"kind": kind, "body": body, "simulated": True, "privacy_note": "No clinical information included. Dispute/assistance pathway available."}
