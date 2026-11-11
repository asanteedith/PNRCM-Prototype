"""Claims Vetting Agent.

Reviews claims before submission and returns a vetting status with detected
issues, severity, recommended corrections, and an explanation for each issue.

Statuses: PASS, PASS_WITH_WARNINGS, NEEDS_CORRECTION, BLOCKED
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Tuple

from db import get_db, now, agent_execution_start, agent_execution_complete, audit

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

STATUS_PRIORITY = {
    "PASS": 0,
    "PASS_WITH_WARNINGS": 1,
    "NEEDS_CORRECTION": 2,
    "BLOCKED": 3,
}


def _issues_severity_max(issues: List[Dict[str, Any]]) -> str:
    max_sev = "low"
    for issue in issues:
        sev = issue.get("severity", "low")
        if SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(max_sev, 0):
            max_sev = sev
    return max_sev


def _status_for(issues: List[Dict[str, Any]]) -> str:
    if not issues:
        return "PASS"
    max_sev = _issues_severity_max(issues)
    critical = any(i.get("severity") == "critical" for i in issues)
    high = any(i.get("severity") == "high" for i in issues)
    if critical:
        return "BLOCKED"
    if high:
        return "NEEDS_CORRECTION"
    return "PASS_WITH_WARNINGS"


def _required_fields_ok(claim: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    required = ["patient_id", "encounter_id", "diagnosis_codes", "procedure_codes", "total_charges"]
    missing = [f for f in required if not claim.get(f)]
    if missing:
        issues.append({
            "code": "MISSING_REQUIRED_FIELDS",
            "severity": "critical",
            "issue": "Required claim fields are missing.",
            "detail": "Missing fields: " + ", ".join(missing),
            "recommended_correction": "Provide all required fields before submission.",
            "explanation": "A claim cannot be adjudicated without patient, encounter, diagnosis, procedure, and charge information.",
        })
    return (len(issues) == 0), issues


def _patient_encounter_consistency(claim: Dict[str, Any], c) -> Tuple[bool, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    pid = claim.get("patient_id")
    eid = claim.get("encounter_id")
    if not pid or not eid:
        return True, issues
    encounter = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    if not encounter:
        issues.append({
            "code": "ENCOUNTER_NOT_FOUND",
            "severity": "high",
            "issue": "Encounter referenced by claim does not exist.",
            "detail": f"Encounter {eid} not found.",
            "recommended_correction": "Verify the encounter identifier and link the claim to a valid encounter.",
            "explanation": "The claim references an encounter that cannot be located in the system.",
        })
    elif encounter["patient_id"] != pid:
        patient = c.execute("SELECT patient_id FROM patients WHERE id=?", (pid,)).fetchone()
        enc_patient = encounter["patient_id"]
        issues.append({
            "code": "PATIENT_ENCOUNTER_MISMATCH",
            "severity": "high",
            "issue": "Claim patient does not match the patient linked to the encounter.",
            "detail": f"Claim patient {pid} differs from encounter patient {enc_patient}.",
            "recommended_correction": "Correct the patient identifier on the claim or re-link to the correct encounter.",
            "explanation": "The claim must be for the same patient as the encounter it references.",
        })
    return (len(issues) == 0), issues


def _encounter_completeness(claim: Dict[str, Any], c) -> Tuple[bool, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    eid = claim.get("encounter_id")
    if not eid:
        return True, issues
    encounter = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    if not encounter:
        return True, issues
    if encounter["status"] not in ("completed", "discharged"):
        issues.append({
            "code": "ENCOUNTER_NOT_COMPLETE",
            "severity": "high",
            "issue": "Encounter is not yet completed.",
            "detail": f"Encounter status is '{encounter['status']}'.",
            "recommended_correction": "Complete the encounter and update its status before submitting the claim.",
            "explanation": "Claims should only be submitted for encounters that have been completed.",
        })
    if not encounter["documentation_complete"]:
        issues.append({
            "code": "DOCUMENTATION_INCOMPLETE",
            "severity": "medium",
            "issue": "Encounter documentation is incomplete.",
            "detail": "Clinical documentation has not been marked complete for this encounter.",
            "recommended_correction": "Complete clinical documentation review before submitting the claim.",
            "explanation": "Incomplete documentation increases the risk of claim denial.",
        })
    return (len(issues) == 0), issues


def _diagnosis_procedure_consistency(claim: Dict[str, Any], c) -> Tuple[bool, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    dx = claim.get("diagnosis_codes")
    proc = claim.get("procedure_codes")
    if not dx or not proc:
        return True, issues
    try:
        dx_list = json.loads(dx) if isinstance(dx, str) else dx
        proc_list = json.loads(proc) if isinstance(proc, str) else proc
    except (json.JSONDecodeError, TypeError):
        issues.append({
            "code": "INVALID_CODE_FORMAT",
            "severity": "medium",
            "issue": "Diagnosis or procedure codes are not valid JSON arrays.",
            "detail": "Codes must be provided as JSON arrays of strings.",
            "recommended_correction": "Reformat diagnosis and procedure codes as JSON arrays.",
            "explanation": "Malformed code lists cannot be validated for consistency.",
        })
        return (len(issues) == 0), issues
    if not dx_list or not proc_list:
        return True, issues
    return True, issues


def _duplicate_claim(claim: Dict[str, Any], c) -> Tuple[bool, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    eid = claim.get("encounter_id")
    payer = claim.get("payer_type")
    if not eid or not payer:
        return True, issues
    encounter = c.execute("SELECT id FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    enc_id = encounter["id"] if encounter else eid
    existing = c.execute(
        "SELECT id, claim_number, status FROM claims WHERE (encounter_id=? OR encounter_id=?) AND payer_type=? AND id<>?",
        (enc_id, eid, payer, claim.get("id", "")),
    ).fetchall()
    if existing:
        issues.append({
            "code": "DUPLICATE_CLAIM",
            "severity": "high",
            "issue": "A claim already exists for this encounter and payer.",
            "detail": f"Existing claim(s): " + ", ".join(r["claim_number"] or r["id"] for r in existing),
            "recommended_correction": "Do not resubmit; follow up on the existing claim instead.",
            "explanation": "Duplicate claims for the same encounter and payer are likely to be rejected.",
        })
    return (len(issues) == 0), issues


def _missing_charges(claim: Dict[str, Any], c) -> Tuple[bool, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    eid = claim.get("encounter_id")
    if not eid:
        return True, issues
    encounter = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    if not encounter:
        return True, issues
    unbilled = c.execute(
        "SELECT id, service_name FROM services WHERE encounter_id=? AND billed=0",
        (encounter["id"],),
    ).fetchall()
    if unbilled:
        issues.append({
            "code": "MISSING_CHARGES",
            "severity": "medium",
            "issue": "Delivered services on the encounter have not been billed.",
            "detail": "Unbilled services: " + ", ".join(r["service_name"] for r in unbilled),
            "recommended_correction": "Add charges for all delivered services before submitting the claim.",
            "explanation": "Billing only a subset of delivered services under-represents the encounter and risks rejection.",
        })
    return (len(issues) == 0), issues


def _valid_billing_values(claim: Dict[str, Any], c) -> Tuple[bool, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    total = claim.get("total_charges")
    if total is None:
        return True, issues
    try:
        total = float(total)
    except (TypeError, ValueError):
        issues.append({
            "code": "INVALID_TOTAL",
            "severity": "medium",
            "issue": "Total charges is not a valid numeric value.",
            "detail": f"Received: {total!r}",
            "recommended_correction": "Provide a valid numeric total charge amount.",
            "explanation": "Non-numeric totals cannot be processed by the payer.",
        })
        return (len(issues) == 0), issues
    if total <= 0:
        issues.append({
            "code": "INVALID_TOTAL",
            "severity": "medium",
            "issue": "Total charges must be greater than zero.",
            "detail": f"Received: {total}",
            "recommended_correction": "Correct the total charge amount.",
            "explanation": "Zero or negative charges are not billable.",
        })
    eid = claim.get("encounter_id")
    if eid:
        encounter = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
        if encounter:
            billed_sum = c.execute(
                "SELECT COALESCE(SUM(total_price),0) s FROM services WHERE encounter_id=? AND billed=1",
                (encounter["id"],),
            ).fetchone()["s"]
            if billed_sum > 0 and abs(total - billed_sum) > 0.01:
                issues.append({
                    "code": "TOTAL_MISMATCH",
                    "severity": "medium",
                    "issue": "Claim total does not match summed billed service charges.",
                    "detail": f"Claim total {total} vs billed services {billed_sum}.",
                    "recommended_correction": "Reconcile the claim total with billed service charges.",
                    "explanation": "The claim total should equal the sum of billed services on the encounter.",
                })
    return (len(issues) == 0), issues


def vet_claim(claim_id: str) -> Dict[str, Any]:
    """Vet a claim and persist the vetting result. Returns the vetting record."""
    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        c.close()
        return {"error": "Claim not found"}, 404

    claim_dict = dict(claim)
    checks = [
        _required_fields_ok(claim_dict),
        _patient_encounter_consistency(claim_dict, c),
        _encounter_completeness(claim_dict, c),
        _diagnosis_procedure_consistency(claim_dict, c),
        _duplicate_claim(claim_dict, c),
        _missing_charges(claim_dict, c),
        _valid_billing_values(claim_dict, c),
    ]

    issues: List[Dict[str, Any]] = []
    for ok, chk_issues in checks:
        issues.extend(chk_issues)

    status = _status_for(issues)
    vetting_id = str(uuid.uuid4())
    c.execute(
        "INSERT INTO claim_vetting(id, claim_id, vetting_status, issues, created_at) VALUES(?,?,?,?,?)",
        (vetting_id, claim["id"], status, json.dumps(issues), now()),
    )
    previous = claim_dict.get("status")
    c.execute("UPDATE claims SET status=?, updated_at=? WHERE id=?", ("vetted", now(), claim["id"]))
    audit(
        c,
        "Claims Vetting Agent",
        "agent",
        "Claims Vetting",
        "Claim vetted",
        "claim",
        claim["id"],
        previous_state=previous,
        new_state="vetted",
        recommendation=status,
        decision=None,
    )
    c.commit()
    c.close()
    return {
        "vetting_id": vetting_id,
        "claim_id": claim["id"],
        "claim_number": claim["claim_number"],
        "vetting_status": status,
        "issues": issues,
        "issue_count": len(issues),
    }


def run_standards_rules(claim_id: str) -> Dict[str, Any]:
    """Evaluate a claim against the configurable payer rules engine.

    All rules are clearly labeled as configurable simulation rules, not official
    payer standards.
    """
    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not claim:
        c.close()
        return {"error": "Claim not found"}, 404

    rules = c.execute(
        "SELECT * FROM payer_rules WHERE is_active=1 ORDER BY severity DESC"
    ).fetchall()

    triggered: List[Dict[str, Any]] = []
    claim_dict = dict(claim)

    for rule in rules:
        condition = json.loads(rule["condition"] or "{}")
        fired = False
        detail = ""

        rtype = rule["rule_type"]
        if rtype == "required_field":
            fields = condition.get("fields", [])
            missing = [f for f in fields if not claim_dict.get(f)]
            if missing:
                fired = True
                detail = "Missing required fields: " + ", ".join(missing)
        elif rtype == "consistency":
            check = condition.get("check")
            if check == "patient_encounter_match":
                pid = claim_dict.get("patient_id")
                eid = claim_dict.get("encounter_id")
                enc = c.execute("SELECT patient_id FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
                if not enc or enc["patient_id"] != pid:
                    fired = True
                    detail = "Claim patient does not match encounter patient."
            elif check == "encounter_completed":
                eid = claim_dict.get("encounter_id")
                enc = c.execute("SELECT status FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
                if not enc or enc["status"] not in ("completed", "discharged"):
                    fired = True
                    detail = "Encounter is not completed."
            elif check == "diagnosis_procedure_match":
                # Placeholder consistency check; no codes to cross-reference in demo.
                fired = False
        elif rtype == "duplicate":
            eid = claim_dict.get("encounter_id")
            payer = claim_dict.get("payer_type")
            if eid and payer:
                enc = c.execute("SELECT id FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
                enc_id = enc["id"] if enc else eid
                existing = c.execute(
                    "SELECT id FROM claims WHERE (encounter_id=? OR encounter_id=?) AND payer_type=? AND id<>?",
                    (enc_id, eid, payer, claim_dict.get("id", "")),
                ).fetchall()
                if existing:
                    fired = True
                    detail = "Duplicate claim detected for same encounter and payer."
        elif rtype == "value_range":
            check = condition.get("check")
            if check == "no_unbilled_services":
                eid = claim_dict.get("encounter_id")
                if eid:
                    enc = c.execute("SELECT id FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
                    if enc:
                        unbilled = c.execute(
                            "SELECT id FROM services WHERE encounter_id=? AND billed=0", (enc["id"],)
                        ).fetchall()
                        if unbilled:
                            fired = True
                            detail = "Unbilled services exist on the encounter."
            elif check == "valid_totals":
                total = claim_dict.get("total_charges")
                try:
                    total = float(total)
                except (TypeError, ValueError):
                    total = None
                if total is None or total <= 0:
                    fired = True
                    detail = "Total charges invalid or not positive."

        if fired:
            triggered.append({
                "rule_id": rule["id"],
                "rule_name": rule["rule_name"],
                "rule_type": rule["rule_type"],
                "severity": rule["severity"],
                "description": rule["description"],
                "triggered_detail": detail,
                "is_simulated": bool(rule["is_simulated"]),
                "note": "This is a configurable simulation rule, not an official payer standard.",
            })

    overall = "PASS"
    if any(t["severity"] == "critical" for t in triggered):
        overall = "BLOCKED"
    elif any(t["severity"] == "high" for t in triggered):
        overall = "NEEDS_CORRECTION"
    elif triggered:
        overall = "PASS_WITH_WARNINGS"

    c.execute("UPDATE claims SET updated_at=? WHERE id=?", (now(), claim["id"]))
    audit(
        c,
        "Standards & Payer Rules Agent",
        "agent",
        "Standards Review",
        "Claim evaluated against payer rules",
        "claim",
        claim["id"],
        previous_state=None,
        new_state="rules_evaluated",
        recommendation=overall,
        decision=None,
    )
    c.commit()
    c.close()
    return {
        "claim_id": claim["id"],
        "claim_number": claim["claim_number"],
        "overall_status": overall,
        "rules_triggered": triggered,
        "rules_count": len(triggered),
        "simulation_notice": "All rules are configurable simulation rules. Replace with verified official payer rules before production use.",
    }
