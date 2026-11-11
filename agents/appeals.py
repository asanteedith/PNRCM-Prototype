"""Claims Appeals & Follow-Up Agent.

Handles denied/rejected/underpaid claims: categorizes denial, generates draft
appeal letters and checklists, tracks appeal lifecycle and follow-up tasks.
All outputs are drafts requiring human review; no fabricated evidence.
"""

from __future__ import annotations
import json, uuid, datetime
from typing import Any, Dict, List, Tuple
from db import get_db, now, audit, agent_execution_start, agent_execution_complete, add_exception

DENIAL_CATEGORIES = [
    "Missing documentation", "Coding inconsistency", "Eligibility issue",
    "Duplicate claim", "Incorrect billing information", "Authorization issue",
    "Missing charges or supporting records", "Late submission", "Underpayment", "Other or unknown",
]

APPEAL_STATUSES = [
    "DETECTED","INVESTIGATING","DRAFT_READY","PENDING_HUMAN_REVIEW",
    "APPROVED_FOR_SUBMISSION","SUBMITTED","AWAITING_RESPONSE",
    "APPROVED","DENIED","RESUBMISSION_REQUIRED","RESOLVED",
]

def _categorize(reason: str) -> str:
    r = (reason or "").lower()
    if "document" in r or "support" in r: return "Missing documentation"
    if "code" in r or "coding" in r: return "Coding inconsistency"
    if "eligib" in r or "coverage" in r: return "Eligibility issue"
    if "duplicate" in r: return "Duplicate claim"
    if "auth" in r: return "Authorization issue"
    if "charge" in r or "unbill" in r: return "Missing charges or supporting records"
    if "late" in r or "timely" in r: return "Late submission"
    if "underpay" in r or "partial" in r: return "Underpayment"
    if "billing" in r or "invalid" in r or "incorrect" in r: return "Incorrect billing information"
    return "Other or unknown"

def _draft_letter(claim: dict, encounter: dict, patient: dict, category: str) -> Tuple[str, List[str]]:
    payer = claim.get("payer_name") or claim.get("payer_type") or "Payer"
    dos = (encounter or {}).get("admission_date") or (encounter or {}).get("created_at") or "N/A"
    reason = claim.get("denial_reason") or "Not specified"
    checklist = ["Claim submission record", "Encounter summary"]
    if category == "Missing documentation":
        checklist += ["Clinical notes", "Supporting diagnostic reports", "Signed orders (if applicable)"]
        justification = "Requested supporting documentation is being compiled for review and resubmission."
    elif category == "Coding inconsistency":
        checklist += ["Coding worksheet", "Diagnosis/procedure code crosswalk"]
        justification = "Coding review is requested to align documented services with billed codes."
    elif category == "Authorization issue":
        checklist += ["Authorization reference", "Authorization correspondence"]
        justification = "Authorization evidence is being reviewed for inclusion with the claim."
    else:
        checklist += ["Relevant supporting documents per denial reason"]
        justification = "Claim details are under review to address the stated denial reason."
    letter = f"""[SIMULATED APPEAL DRAFT - REQUIRES AUTHORIZED HUMAN REVIEW]

To: {payer}
Re: Claim Appeal (Draft)
Claim Reference: {claim.get('claim_number') or claim.get('id')}
Patient Identifier (synthetic): {(patient or {}).get('patient_id') or claim.get('patient_id')}
Encounter Reference: {(encounter or {}).get('encounter_id') or claim.get('encounter_id')}
Date of Service: {dos}

Denial Reason (as recorded): {reason}
Denial Category: {category}

Summary of Issue:
The claim was flagged in the PNRCM demo workflow under the category "{category}". This draft summarizes available record references for human review.

Supporting Evidence (from synthetic records):
- Claim total charges: GHS {float(claim.get('total_charges') or 0):.2f}
- Claim status: {claim.get('status')}
- Encounter status: {(encounter or {}).get('status')}
- Diagnosis codes (recorded): {claim.get('diagnosis_codes') or 'N/A'}
- Procedure codes (recorded): {claim.get('procedure_codes') or 'N/A'}

Proposed Correction/Justification (draft):
{justification}

Requested Action:
Please review the corrected/resubmitted claim information and supporting documentation once approved by an authorized reviewer.

Required Supporting Documents Checklist:
- """ + "\n- ".join(checklist) + """

Note: This is a simulated draft generated from synthetic demo data. It must not be submitted without authorized human review and verification. No clinical facts or payer rules are fabricated.
"""
    return letter, checklist

def detect_and_create_appeals(claim_id: str = None) -> Dict[str, Any]:
    c = get_db()
    if claim_id:
        claims = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchall()
    else:
        claims = c.execute("SELECT * FROM claims WHERE status IN ('denied','rejected','underpaid')").fetchall()
    created = []
    for cl in claims:
        cld = dict(cl)
        exists = c.execute("SELECT id FROM appeals WHERE claim_id=? AND status NOT IN ('RESOLVED','DENIED')", (cld["id"],)).fetchone()
        if exists:
            continue
        enc = c.execute("SELECT * FROM encounters WHERE id=?", (cld.get("encounter_id"),)).fetchone()
        pat = c.execute("SELECT * FROM patients WHERE id=?", (cld.get("patient_id"),)).fetchone()
        encd = dict(enc) if enc else {}
        patd = dict(pat) if pat else {}
        category = _categorize(cld.get("denial_reason") or cld.get("status") or "")
        letter, checklist = _draft_letter(cld, encd, patd, category)
        aid = str(uuid.uuid4())
        amount = float(cld.get("total_charges") or 0) - float(cld.get("paid_amount") or 0)
        deadline = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).date().isoformat()
        c.execute(
            """INSERT INTO appeals(id,claim_id,denial_category,status,payer_name,denial_reason,appeal_letter,supporting_docs,amount_under_appeal,deadline,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, cld["id"], category, "DRAFT_READY", cld.get("payer_name"), cld.get("denial_reason"),
             letter, json.dumps(checklist), max(amount,0), deadline, now(), now())
        )
        audit(c, "Claims Appeals Agent", "agent", "Claims Appeals", "Appeal draft created", "appeal", aid,
              previous_state=None, new_state="DRAFT_READY", recommendation=category)
        add_exception(c, "claims_rejection", "high", f"Appeal draft ready for {cld.get('claim_number')}",
                      f"Claim {cld.get('claim_number')} requires appeal review: {category}",
                      "Review draft, attach evidence, and approve for submission", "Claims Appeals Agent",
                      patient_id=cld.get("patient_id"), encounter_id=cld.get("encounter_id"), claim_id=cld["id"])
        created.append({"appeal_id": aid, "claim_id": cld["id"], "claim_number": cld.get("claim_number"), "category": category, "status": "DRAFT_READY"})
    c.commit(); c.close()
    return {"created": created, "count": len(created)}

def list_appeals(filters: dict = None) -> List[dict]:
    c = get_db()
    rows = c.execute("SELECT a.*, cl.claim_number, cl.total_charges, cl.status as claim_status FROM appeals a LEFT JOIN claims cl ON a.claim_id=cl.id ORDER BY a.created_at DESC").fetchall()
    c.close()
    items = [dict(r) for r in rows]
    f = filters or {}
    if f.get("status"): items = [x for x in items if x["status"] == f["status"]]
    if f.get("payer"): items = [x for x in items if (x.get("payer_name") or "").lower().find(f["payer"].lower()) >= 0]
    if f.get("denial_category"): items = [x for x in items if x.get("denial_category") == f["denial_category"]]
    return items

def get_appeal(aid: str):
    c = get_db()
    r = c.execute("SELECT a.*, cl.claim_number, cl.total_charges, cl.patient_id, cl.encounter_id FROM appeals a LEFT JOIN claims cl ON a.claim_id=cl.id WHERE a.id=?", (aid,)).fetchone()
    c.close()
    return dict(r) if r else None

def update_appeal_status(aid: str, status: str, actor: str = "User", notes: str = "") -> Dict[str, Any]:
    if status not in APPEAL_STATUSES:
        return {"error": "Invalid status"}
    c = get_db()
    r = c.execute("SELECT * FROM appeals WHERE id=?", (aid,)).fetchone()
    if not r:
        c.close(); return {"error": "Appeal not found"}
    prev = r["status"]
    resolved = now() if status in ("RESOLVED","APPROVED","DENIED") else r["resolved_at"]
    c.execute("UPDATE appeals SET status=?, updated_at=?, resolved_at=? WHERE id=?", (status, now(), resolved, aid))
    audit(c, actor, "user", "Claims Appeals", f"Appeal status changed to {status}", "appeal", aid, previous_state=prev, new_state=status, decision=status)
    c.commit(); c.close()
    return {"ok": True, "status": status}

def appeal_metrics() -> Dict[str, Any]:
    c = get_db()
    rows = c.execute("SELECT status, amount_under_appeal, created_at, resolved_at FROM appeals").fetchall()
    c.close()
    total = len(rows)
    pending_review = sum(1 for r in rows if r["status"] in ("DRAFT_READY","PENDING_HUMAN_REVIEW"))
    submitted = sum(1 for r in rows if r["status"] in ("SUBMITTED","AWAITING_RESPONSE"))
    approved = sum(1 for r in rows if r["status"] == "APPROVED")
    denied = sum(1 for r in rows if r["status"] == "DENIED")
    amount_under = sum(float(r["amount_under_appeal"] or 0) for r in rows if r["status"] not in ("RESOLVED","APPROVED","DENIED"))
    # recovered after appeal = amount of approved appeals (simulated)
    recovered = sum(float(r["amount_under_appeal"] or 0) for r in rows if r["status"] == "APPROVED")
    return {
        "total_appeals": total, "pending_review": pending_review, "submitted": submitted,
        "approved": approved, "denied": denied, "amount_under_appeal": round(amount_under,2),
        "amount_recovered": round(recovered,2),
        "success_rate": round((approved/total*100) if total else 0, 1)
    }
