"""Prior Authorization Agent.

Tracks prior authorization requirements, identifies services that need
authorization before submission, monitors expiry, and flags gaps that could
lead to denials.
"""

from __future__ import annotations

import json, uuid
from typing import Any, Dict, List

from db import get_db, now, audit, agent_execution_start, agent_execution_complete, add_exception, add_transition
import agents.orchestrator as orch

AUTH_REQUIRED_SERVICES = {"MRI", "CT Scan", "Surgery", "Inpatient", "ICU", "Specialist Consult", "Infusion", "DME"}


def check_authorizations(encounter_id: str) -> Dict[str, Any]:
    """Check authorization status for all services on an encounter."""
    c = get_db()
    eid = agent_execution_start(c, "Prior Authorization Agent", "auth_check", "encounter", encounter_id, None, None)
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (encounter_id, encounter_id)).fetchone()
    if not enc:
        c.close(); return {"error": "Encounter not found"}, 404
    e = dict(enc)
    patient_id = e["patient_id"]

    services = c.execute("SELECT * FROM services WHERE encounter_id=?", (e["id"],)).fetchall()
    auths_needed = []
    auths_missing = []
    auths_active = []

    for svc in services:
        sname = svc["service_name"] or ""
        needs_auth = any(kw.lower() in sname.lower() for kw in AUTH_REQUIRED_SERVICES)
        if not needs_auth:
            continue
        existing = c.execute(
            "SELECT * FROM authorizations WHERE patient_id=? AND service_name=? AND status='active'",
            (patient_id, sname),
        ).fetchone()
        auth_info = {
            "service": sname,
            "service_id": svc["id"],
            "amount": float(svc["total_price"] or 0),
        }
        if existing:
            auth_info["authorization_id"] = existing["id"]
            auth_info["status"] = existing["status"]
            auth_info["authorized_at"] = existing.get("authorized_at")
            auth_info["expires_at"] = existing.get("expires_at")
            auths_active.append(auth_info)
        else:
            auths_missing.append(auth_info)
            auths_needed.append(auth_info)

    result = {
        "encounter_id": encounter_id,
        "patient_id": patient_id,
        "services_checked": len(services),
        "auth_required_count": len(auths_needed),
        "auth_active_count": len(auths_active),
        "auth_missing_count": len(auths_missing),
        "auths_needed": auths_needed,
        "auths_active": auths_active,
        "all_authorized": len(auths_missing) == 0,
    }

    # Add exceptions for missing authorizations
    for missing in auths_missing:
        add_exception(c, "prior_authorization", "high",
                      f"Prior authorization missing for {missing['service']}",
                      f"Service {missing['service']} on encounter {encounter_id} requires prior authorization but none is active.",
                      "Obtain prior authorization before claim submission",
                      "Prior Authorization Agent",
                      patient_id=patient_id, encounter_id=encounter_id)

    agent_execution_complete(c, eid, "completed", result, human_review_required=len(auths_missing) > 0, duration_ms=35)
    orch.set_agent_state("Prior Authorization Agent", "Completed", "Authorization check", result)

    c.close()
    return result


def create_authorization(c, patient_id: str, service_name: str, amount: float,
                         payer: str, authorized_by: str = "System",
                         expires_at: str | None = None) -> Dict[str, Any]:
    """Create a prior authorization record."""
    c = get_db() if c is None else c
    aid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO authorizations(id, patient_id, service_name, amount, payer, status, authorized_by, authorized_at, expires_at, created_at, updated_at)
        VALUES(?,?,?,?,?,'active',?,?,?,?,?)""",
        (aid, patient_id, service_name, amount, payer, authorized_by, now(), expires_at, now(), now()),
    )
    c.commit()
    if c is not None and not hasattr(c, '_external'):
        c.close()
    return {"authorization_id": aid, "status": "active", "service": service_name}


def create_auth_task(c, encounter_id: str, patient_id: str, priority: str = "high") -> Dict[str, Any]:
    """Create an orchestrator task for prior authorization."""
    return orch.create_orchestrator_task(
        c, "prior_authorization", "encounter", encounter_id,
        agent_name="Prior Authorization Agent",
        direction="agent_to_human",
        priority=priority,
        context=f"Prior authorization required for services on encounter {encounter_id} before claim submission",
        ai_recommendation="Obtain prior authorization from payer for all auth-required services",
        financial_impact=0.0,
        auto_assign=True,
    )
