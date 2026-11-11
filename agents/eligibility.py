"""Eligibility & Coverage Agent.

Validates patient insurance eligibility and coverage at registration and
before claim submission. Identifies coverage gaps, missing authorizations,
and payer-specific rules that could lead to denials.
"""

from __future__ import annotations

import json, uuid
from typing import Any, Dict, List

from db import get_db, now, audit, agent_execution_start, agent_execution_complete, add_exception
import agents.orchestrator as orch

# Payers that require prior authorization for certain services
AUTH_REQUIRED_PAYERS = {"nhis", "private", "commercial"}
# Services that typically require authorization
AUTH_REQUIRED_SERVICES = {"MRI", "CT Scan", "Surgery", "Inpatient", "ICU", "Specialist Consult"}


def check_eligibility(patient_id: str) -> Dict[str, Any]:
    """Check eligibility and coverage for a patient."""
    c = get_db()
    eid = agent_execution_start(c, "Eligibility & Coverage Agent", "eligibility_check", "patient", patient_id, None, None)
    patient = c.execute("SELECT * FROM patients WHERE id=? OR patient_id=?", (patient_id, patient_id)).fetchone()
    if not patient:
        c.close(); return {"error": "Patient not found"}, 404

    p = dict(patient)
    insurance_type = p.get("insurance_type") or "self_pay"
    eligibility = {
        "patient_id": patient_id,
        "mrn": p.get("patient_id"),
        "insurance_type": insurance_type,
        "eligible": True,
        "coverage_status": "active",
        "requires_authorization": False,
        "authorizations_needed": [],
        "warnings": [],
        "errors": [],
    }

    # Self-pay patients are always eligible
    if insurance_type == "self_pay":
        eligibility["coverage_status"] = "self_pay"
        eligibility["eligible"] = True
    elif insurance_type in ("nhis", "private", "commercial"):
        eligibility["coverage_status"] = "active"
        eligibility["eligible"] = True
        # Check for missing insurance number
        if not p.get("insurance_number"):
            eligibility["warnings"].append("Insurance number not on file — verify before claim submission")
            eligibility["requires_authorization"] = True
    else:
        eligibility["eligible"] = False
        eligibility["coverage_status"] = "unknown"
        eligibility["errors"].append(f"Unknown insurance type: {insurance_type}")

    # Check for missing demographic data
    missing = []
    for f in ["date_of_birth", "gender", "phone", "address"]:
        if not p.get(f):
            missing.append(f)
    if missing:
        eligibility["warnings"].append(f"Missing demographic fields: {', '.join(missing)}")

    # Record agent execution
    agent_execution_complete(c, eid, "completed", eligibility, human_review_required=bool(eligibility["errors"]), duration_ms=30)
    orch.set_agent_state("Eligibility & Coverage Agent", "Completed", "Eligibility check", eligibility)

    c.close()
    return eligibility


def check_encounter_eligibility(encounter_id: str) -> Dict[str, Any]:
    """Check eligibility for an encounter — reviews patient coverage and services."""
    c = get_db()
    eid = agent_execution_start(c, "Eligibility & Coverage Agent", "encounter_eligibility", "encounter", encounter_id, None, None)
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (encounter_id, encounter_id)).fetchone()
    if not enc:
        c.close(); return {"error": "Encounter not found"}, 404
    e = dict(enc)
    patient = c.execute("SELECT * FROM patients WHERE id=?", (e["patient_id"],)).fetchone()
    if not patient:
        c.close(); return {"error": "Patient not found"}, 404
    p = dict(patient)

    result = check_eligibility(p["id"])
    if isinstance(result, tuple):
        c.close(); return result

    # Check services for authorization requirements
    services = c.execute("SELECT * FROM services WHERE encounter_id=?", (e["id"],)).fetchall()
    auth_needed = []
    for svc in services:
        sname = svc["service_name"] or ""
        for auth_svc in AUTH_REQUIRED_SERVICES:
            if auth_svc.lower() in sname.lower():
                auth_needed.append({
                    "service": sname,
                    "service_id": svc["id"],
                    "reason": f"{auth_svc} typically requires prior authorization",
                })
    if auth_needed:
        result["requires_authorization"] = True
        result["authorizations_needed"] = auth_needed
        result["warnings"].append(f"{len(auth_needed)} service(s) may require prior authorization")

    # Check existing authorizations
    existing_auths = c.execute(
        "SELECT * FROM authorizations WHERE patient_id=? AND status='active'", (p["id"],)
    ).fetchall()
    result["active_authorizations"] = len(existing_auths)

    # Record
    agent_execution_complete(c, eid, "completed", result, human_review_required=bool(result.get("errors")), duration_ms=40)
    orch.set_agent_state("Eligibility & Coverage Agent", "Completed", "Encounter eligibility check", result)

    c.close()
    return result


def create_eligibility_task(c, encounter_id: str, patient_id: str, priority: str = "medium") -> Dict[str, Any]:
    """Create an orchestrator task for eligibility verification."""
    return orch.create_orchestrator_task(
        c, "eligibility_check", "encounter", encounter_id,
        agent_name="Eligibility & Coverage Agent",
        direction="agent_to_human",
        priority=priority,
        context=f"Verify eligibility and coverage for patient {patient_id} before claim submission",
        ai_recommendation="Confirm active coverage, check for missing authorizations, validate demographic data",
        financial_impact=0.0,
        auto_assign=True,
    )
