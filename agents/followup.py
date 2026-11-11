"""Claims Follow-Up tracking agent."""
from __future__ import annotations
import json, uuid, datetime
from typing import Any, Dict, List
from db import get_db, now, audit, add_exception

FOLLOWUP_STATUSES = [
    "SUBMITTED","AWAITING_PAYER_RESPONSE","FOLLOW_UP_DUE","FOLLOW_UP_IN_PROGRESS",
    "RESPONSE_RECEIVED","APPROVED","DENIED","UNDERPAID","RESUBMISSION_REQUIRED","RESOLVED"
]

def ensure_followup(claim_id: str, assigned_staff: str = "Unassigned", priority: str = "medium") -> Dict[str, Any]:
    c = get_db()
    cl = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not cl:
        c.close(); return {"error": "Claim not found"}
    existing = c.execute("SELECT id FROM claim_followups WHERE claim_id=?", (cl["id"],)).fetchone()
    if existing:
        c.close(); return {"followup_id": existing["id"], "existing": True}
    fid = str(uuid.uuid4())
    due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)).date().isoformat()
    c.execute(
        """INSERT INTO claim_followups(id,claim_id,status,priority,assigned_staff,due_date,next_followup_date,created_at,updated_at)
           VALUES(?,?,'AWAITING_PAYER_RESPONSE',?,?,?,?,?,?)""",
        (fid, cl["id"], priority, assigned_staff, due, due, now(), now())
    )
    audit(c, "Claims Follow-Up Agent", "agent", "Claims Follow-Up", "Follow-up task created", "claim_followup", fid, new_state="AWAITING_PAYER_RESPONSE")
    c.commit(); c.close()
    return {"followup_id": fid, "existing": False}

def list_followups(filters: dict = None) -> List[dict]:
    c = get_db()
    rows = c.execute(
        """SELECT f.*, c.claim_number, c.payer_name, c.total_charges, c.status as claim_status
           FROM claim_followups f LEFT JOIN claims c ON f.claim_id=c.id ORDER BY f.updated_at DESC"""
    ).fetchall()
    c.close()
    items = [dict(r) for r in rows]
    f = filters or {}
    if f.get("status"): items = [x for x in items if x["status"] == f["status"]]
    if f.get("payer"): items = [x for x in items if (x.get("payer_name") or "").lower().find(f["payer"].lower()) >= 0]
    if f.get("priority"): items = [x for x in items if x.get("priority") == f["priority"]]
    return items

def update_followup(fid: str, updates: dict, actor: str = "User") -> Dict[str, Any]:
    c = get_db()
    r = c.execute("SELECT * FROM claim_followups WHERE id=?", (fid,)).fetchone()
    if not r:
        c.close(); return {"error": "Follow-up not found"}
    allowed = {"status","priority","assigned_staff","due_date","next_followup_date","notes","last_contact_date"}
    sets, vals = [], []
    for k,v in updates.items():
        if k in allowed:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        c.close(); return {"error": "No valid fields"}
    sets.append("updated_at=?"); vals.append(now()); vals.append(fid)
    c.execute(f"UPDATE claim_followups SET {', '.join(sets)} WHERE id=?", vals)
    audit(c, actor, "user", "Claims Follow-Up", "Follow-up updated", "claim_followup", fid, previous_state=r["status"], new_state=updates.get("status", r["status"]))
    c.commit(); c.close()
    return {"ok": True}

def add_attempt(fid: str, method: str, note: str, outcome: str = "") -> Dict[str, Any]:
    c = get_db()
    r = c.execute("SELECT * FROM claim_followups WHERE id=?", (fid,)).fetchone()
    if not r:
        c.close(); return {"error": "Follow-up not found"}
    aid = str(uuid.uuid4())
    c.execute("INSERT INTO claim_followup_attempts(id,followup_id,ts,method,note,outcome) VALUES(?,?,?,?,?,?)",
              (aid, fid, now(), method, note, outcome))
    c.execute("UPDATE claim_followups SET last_contact_date=?, updated_at=? WHERE id=?", (now(), now(), fid))
    c.commit(); c.close()
    return {"attempt_id": aid}

def detect_overdue(days_threshold: int = 14) -> List[dict]:
    c = get_db()
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_threshold)).isoformat()
    rows = c.execute(
        """SELECT f.*, c.claim_number, c.total_charges, c.payer_name FROM claim_followups f
           JOIN claims c ON f.claim_id=c.id
           WHERE f.status NOT IN ('RESOLVED','APPROVED') AND (f.due_date < ? OR c.submitted_at < ?)""",
        (datetime.date.today().isoformat(), cutoff)
    ).fetchall()
    overdue = []
    for r in rows:
        rd = dict(r)
        if rd["status"] != "FOLLOW_UP_DUE":
            c.execute("UPDATE claim_followups SET status='FOLLOW_UP_DUE', updated_at=? WHERE id=?", (now(), rd["id"]))
        add_exception(c, "claims_followup", "medium", f"Overdue follow-up: {rd.get('claim_number')}",
                      f"Claim {rd.get('claim_number')} requires follow-up (threshold {days_threshold} days).",
                      "Contact payer, record attempt, escalate if needed", "Claims Follow-Up Agent", claim_id=rd["claim_id"])
        overdue.append(rd)
    c.commit(); c.close()
    return overdue

def followup_metrics() -> Dict[str, Any]:
    c = get_db()
    rows = c.execute("SELECT status FROM claim_followups").fetchall()
    today = datetime.date.today().isoformat()
    overdue = c.execute("SELECT COUNT(*) n FROM claim_followups WHERE status NOT IN ('RESOLVED','APPROVED') AND due_date < ?", (today,)).fetchone()["n"]
    awaiting = sum(1 for r in rows if r["status"] == "AWAITING_PAYER_RESPONSE")
    c.close()
    return {"total": len(rows), "awaiting_response": awaiting, "overdue": overdue}
