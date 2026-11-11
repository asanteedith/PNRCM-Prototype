"""Clinical Documentation and Coding Review Agent.

Reviews clinical documentation for missing or inconsistent information,
suggests diagnosis documentation corrections, flags coding inconsistencies,
identifies missing supporting documentation, and explains each recommendation.

Requires authorized human review before changing clinical documentation or codes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Tuple

from db import get_db, now, agent_execution_start, agent_execution_complete, audit

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _findings_for_encounter(encounter: Dict[str, Any], services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []

    # Missing reason / chief complaint
    if not encounter.get("reason"):
        findings.append({
            "code": "MISSING_CHIEF_COMPLAINT",
            "severity": "high",
            "finding": "Chief complaint / reason for encounter is not documented.",
            "explanation": "A documented reason for the encounter is required to support medical necessity and code selection.",
            "recommended_correction": "Document the chief complaint or reason for the encounter.",
            "field": "reason",
        })

    # Missing attending physician
    if not encounter.get("attending_physician"):
        findings.append({
            "code": "MISSING_ATTENDING_PHYSICIAN",
            "severity": "medium",
            "finding": "Attending physician is not documented.",
            "explanation": "The attending physician must be identified for the encounter record.",
            "recommended_correction": "Record the attending physician's name.",
            "field": "attending_physician",
        })

    # Services without documentation link
    for svc in services:
        if not svc.get("description"):
            findings.append({
                "code": "SERVICE_DESCRIPTION_MISSING",
                "severity": "medium",
                "finding": f"Service '{svc['service_name']}' lacks a clinical description.",
                "explanation": "Each delivered service should have supporting clinical documentation describing what was performed and why.",
                "recommended_correction": "Add a clinical description for the service.",
                "field": f"services.{svc['id']}.description",
            })

    # Diagnosis-code vs documentation consistency check
    # Encounter reason present but no diagnosis codes on linked claims — flagged at claim level elsewhere;
    # here we flag when an encounter is completed but documentation is incomplete.
    if encounter.get("status") == "completed" and not encounter.get("documentation_complete"):
        findings.append({
            "code": "ENCOUNTER_DOCUMENTATION_INCOMPLETE",
            "severity": "high",
            "finding": "Encounter is marked complete but clinical documentation is incomplete.",
            "explanation": "Completed encounters require complete documentation before codes can be finalized.",
            "recommended_correction": "Complete clinical documentation review and mark documentation as complete.",
            "field": "documentation_complete",
        })

    return findings


def review_documentation(encounter_id: str) -> Dict[str, Any]:
    """Run the documentation and coding review for an encounter.

    Persists a documentation_review record in `pending` status requiring
    human review before any documentation or code changes are applied.
    """
    c = get_db()
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (encounter_id, encounter_id)).fetchone()
    if not enc:
        c.close()
        return {"error": "Encounter not found"}, 404

    enc_dict = dict(enc)
    services = c.execute(
        "SELECT * FROM services WHERE encounter_id=? ORDER BY created_at", (enc_dict["id"],)
    ).fetchall()
    service_list = [dict(s) for s in services]

    findings = _findings_for_encounter(enc_dict, service_list)
    # Deterministic coding-inconsistency flag: completed encounter with no procedures/diagnoses on record
    claims = c.execute("SELECT * FROM claims WHERE encounter_id=?", (enc_dict["id"],)).fetchall()
    has_codes = any(
        (json.loads(cl["diagnosis_codes"]) if cl["diagnosis_codes"] else [])
        or (json.loads(cl["procedure_codes"]) if cl["procedure_codes"] else [])
        for cl in claims
    )
    if enc_dict["status"] == "completed" and not has_codes:
        findings.append({
            "code": "MISSING_DIAGNOSTIC_CODES",
            "severity": "high",
            "finding": "Completed encounter has no diagnosis or procedure codes documented.",
            "explanation": "Diagnosis and procedure codes are required for billing and claim submission.",
            "recommended_correction": "Assign appropriate diagnosis and procedure codes based on documented clinical findings.",
            "field": "diagnosis_codes",
        })

    review_id = str(uuid.uuid4())
    findings_json = json.dumps(findings)
    # Determine overall review status
    if not findings:
        status = "approved"
        human_required = 0
    else:
        status = "pending"
        human_required = 1

    c.execute(
        """INSERT INTO documentation_reviews(id, encounter_id, review_type, findings, status, human_review_required, reviewer_decision, reviewer_notes, resolved_at, created_at)
        VALUES(?,?,'clinical',?,?,?,NULL,NULL,NULL,?)""",
        (review_id, enc_dict["id"], findings_json, status, human_required, now()),
    )
    previous = enc_dict.get("documentation_complete")
    new_doc_complete = 1 if status == "approved" else 0
    c.execute("UPDATE encounters SET documentation_complete=?, updated_at=? WHERE id=?", (new_doc_complete, now(), enc_dict["id"]))
    audit(
        c,
        "Documentation Review Agent",
        "agent",
        "Documentation Review",
        "Clinical documentation reviewed",
        "encounter",
        enc_dict["id"],
        previous_state=f"documentation_complete={previous}",
        new_state=f"documentation_complete={new_doc_complete}",
        recommendation=status,
        decision=None,
    )
    c.commit()
    c.close()
    return {
        "review_id": review_id,
        "encounter_id": enc_dict["id"],
        "encounter_number": enc_dict["encounter_id"],
        "review_type": "clinical",
        "status": status,
        "human_review_required": bool(human_required),
        "findings": findings,
        "finding_count": len(findings),
    }


def resolve_documentation_review(review_id: str, decision: str, reviewer_notes: str = "") -> Dict[str, Any]:
    """Human reviewer resolves a documentation review.

    decision: 'approve' | 'reject' | 'corrections_requested'
    """
    if decision not in ("approve", "reject", "corrections_requested"):
        return {"error": "decision must be approve, reject, or corrections_requested"}, 400
    c = get_db()
    rev = c.execute("SELECT * FROM documentation_reviews WHERE id=?", (review_id,)).fetchone()
    if not rev:
        c.close()
        return {"error": "Documentation review not found"}, 404
    if rev["status"] != "pending":
        c.close()
        return {"error": "Documentation review already resolved"}, 409

    enc = c.execute("SELECT * FROM encounters WHERE id=?", (rev["encounter_id"],)).fetchone()
    new_status = rev["status"]
    doc_complete = enc["documentation_complete"] if enc else 0

    if decision == "approve":
        new_status = "approved"
        doc_complete = 1
    elif decision == "reject":
        new_status = "rejected"
        doc_complete = 0
    elif decision == "corrections_requested":
        new_status = "corrections_requested"
        doc_complete = 0

    c.execute(
        """UPDATE documentation_reviews SET status=?, reviewer_decision=?, reviewer_notes=?, resolved_at=? WHERE id=?""",
        (new_status, decision, reviewer_notes, now(), review_id),
    )
    if enc:
        c.execute("UPDATE encounters SET documentation_complete=?, updated_at=? WHERE id=?", (doc_complete, now(), enc["id"]))
    audit(
        c,
        "Human Reviewer",
        "user",
        "Documentation Review",
        f"Documentation review {decision}",
        "documentation_review",
        review_id,
        previous_state="pending",
        new_state=new_status,
        recommendation=rev["findings"],
        decision=decision,
    )
    c.commit()
    c.close()
    return {
        "review_id": review_id,
        "status": new_status,
        "decision": decision,
        "documentation_complete": bool(doc_complete),
    }
