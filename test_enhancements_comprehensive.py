"""Comprehensive test of all enhancement phases using Flask test client."""
import json, sys, datetime
import app

client = app.app.test_client()

results = {"passed": 0, "failed": 0, "errors": []}

def check(name, cond, detail=""):
    if cond:
        results["passed"] += 1
        print("  PASS: " + name + ((" — " + detail) if detail else ""))
    else:
        results["failed"] += 1
        results["errors"].append(name + ": " + str(detail))
        print("  FAIL: " + name + ((" — " + detail) if detail else ""))

def get(path):
    r = client.get(path)
    return r.status_code, r.get_json(silent=True) or {}

def post(path, data=None):
    body = json.dumps(data or {}).encode() if data is not None else None
    r = client.post(path, data=body, content_type="application/json")
    return r.status_code, r.get_json(silent=True) or {}

print("=" * 60)
print("PNRCM Enhancements — Comprehensive Test Suite")
print("=" * 60)

# ---- Phase 1: Command Center ----
print("\n--- Phase 1: Command Center ---")
s, b = get("/api/command-center/overview")
check("Command center overview returns 200", s == 200, str(b)[:100] if s != 200 else "")
if s == 200:
    kpis = b.get("kpis", {})
    required_kpis = [
        "total_charges", "total_claims_submitted", "total_claims_approved",
        "total_collections", "outstanding_accounts_receivable", "ar_aging",
        "denied_claims", "denied_value", "clean_claim_rate", "revenue_at_risk",
        "potential_missed_charges", "claims_requiring_immediate_action",
        "pending_human_reviews", "revenue_recovered", "revenue_protected",
        "collection_rate",
    ]
    for k in required_kpis:
        check("KPI: " + k, k in kpis, "missing" if k not in kpis else "")
    check("Currency is GHS", b.get("currency") == "GHS")
    check("Has counts", "counts" in b)
    check("Has distinctions", "distinctions" in b)

s, b = get("/api/command-center/actions")
check("Command center actions returns 200", s == 200)
if s == 200:
    check("Has actions list", "actions" in b)
    check("Has count", "count" in b)
    if b.get("actions"):
        a = b["actions"][0]
        for field in ["action_id", "issue_description", "reference_type", "reference_id",
                       "responsible_role", "priority", "financial_impact",
                       "recommended_action", "current_status"]:
            check("Action has field: " + field, field in a)

# ---- Phase 2: Revenue Integrity Leakage Findings ----
print("\n--- Phase 2: Revenue Integrity Leakage Findings ---")
s, b = get("/api/revenue-integrity/leakage-findings")
check("Leakage findings returns 200", s == 200)
if s == 200:
    check("Has findings list", "findings" in b)
    check("Has count", "count" in b)
    check("Has total_estimated_impact", "total_estimated_impact" in b)
    check("Has total_confirmed_recoverable", "total_confirmed_recoverable" in b)
    check("Currency is GHS", b.get("currency") == "GHS")
    if b.get("findings"):
        f = b["findings"][0]
        for field in ["finding_id", "category", "evidence", "severity", "confidence",
                       "estimated_financial_impact", "recommended_action",
                       "assigned_owner", "review_status"]:
            check("Finding has field: " + field, field in f)

# ---- Phase 3: Payer Intelligence ----
print("\n--- Phase 3: Payer Intelligence ---")
s, b = get("/api/analytics/payer-intelligence")
check("Payer intelligence returns 200", s == 200)
if s == 200:
    check("Has payers list", "payers" in b)
    check("Has count", "count" in b)
    if b.get("payers"):
        p = b["payers"][0]
        for field in ["payer", "claims_submitted", "claims_accepted", "claims_denied",
                       "denial_rate", "total_collected", "average_claim_value"]:
            check("Payer has field: " + field, field in p)

# ---- Phase 4: Scorecard ----
print("\n--- Phase 4: Performance Scorecard ---")
s, b = get("/api/analytics/scorecard")
check("Scorecard returns 200", s == 200)
if s == 200:
    required_kpis = [
        "clean_claim_rate", "denial_rate", "ar_days", "collection_rate",
        "charge_capture_rate", "claim_turnaround_time",
        "denial_resolution_time", "revenue_recovery",
    ]
    for k in required_kpis:
        check("Scorecard KPI: " + k, k in b.get("kpis", {}), "missing" if k not in b.get("kpis", {}) else "")
    if b.get("kpis"):
        kpi = b["kpis"]["clean_claim_rate"]
        for field in ["value", "unit", "definition", "calculation", "source"]:
            check("KPI has field: " + field, field in kpi)
    check("Has data_quality", "data_quality" in b)
    check("Currency is GHS", b.get("currency") == "GHS")

# ---- Phase 5: Human Review ----
print("\n--- Phase 5: Human Review & Accountability ---")
s, b = get("/api/review-tasks/enhanced")
check("Enhanced review tasks returns 200", s == 200)
if s == 200:
    check("Has tasks list", "tasks" in b)
    check("Has count", "count" in b)
    check("Has reviewer_workload", "reviewer_workload" in b)
    if b.get("tasks"):
        t = b["tasks"][0]
        for field in ["task_id", "task_type", "status", "priority", "assigned_to",
                       "ai_recommendation", "human_decision", "financial_impact",
                       "overdue", "sla_status", "assignment_history",
                       "escalation_history", "audit_trail"]:
            check("Task has field: " + field, field in t)

# Test resolve review task
s, b = post("/api/review-tasks/enhanced", {
    "task_type": "documentation_review",
    "entity_type": "encounter",
    "entity_id": "test-enc-001",
    "context": "Test review",
    "ai_recommendation": "Approve documentation",
    "financial_impact": 100.0,
    "priority": "medium",
})
check("Create review task", s in (200, 201), str(b)[:100] if s not in (200, 201) else "")

# ---- Phase 6: Revenue Impact Tracking ----
print("\n--- Phase 6: Revenue Impact Tracking ---")
s, b = post("/api/revenue-impact", {
    "source_finding_id": "leak-001",
    "source_finding_type": "leakage_finding",
    "original_issue": "Test underpayment — claim paid less than expected",
    "estimated_amount": 2400.00,
    "related_claim_id": "claim-001",
    "assigned_owner": "Reconciliation Agent",
    "actor": "User",
})
check("Create revenue impact", s in (200, 201), str(b)[:150] if s not in (200, 201) else "")
if s in (200, 201):
    impact_id = b.get("impact_id")
    check("Has impact_id", impact_id is not None)
    check("Lifecycle stage is IDENTIFIED", b.get("lifecycle_stage") == "IDENTIFIED")
    check("Estimated amount", b.get("initial_estimated_amount") == 2400.0)
    check("Confirmed recovered is 0", b.get("confirmed_recovered_amount") == 0.0)

    # Test duplicate prevention
    s2, b2 = post("/api/revenue-impact", {
        "source_finding_id": "leak-001",
        "source_finding_type": "leakage_finding",
        "original_issue": "Duplicate attempt",
        "estimated_amount": 100.0,
    })
    check("Duplicate impact prevented", s2 == 409, str(b2)[:100])

    # Test stage transition
    s3, b3 = post("/api/revenue-impact/" + impact_id + "/stage", {
        "stage": "ESTIMATED",
        "actor": "User",
    })
    check("Transition to ESTIMATED", s3 == 200, str(b3)[:100] if s3 != 200 else "")

    # Test outcome recording
    s4, b4 = post("/api/revenue-impact/" + impact_id + "/outcome", {
        "outcome_category": "revenue_recovered",
        "confirmed_amount": 1800.00,
        "actor": "User",
        "verified_by": "Reviewer",
    })
    check("Record revenue_recovered outcome", s4 == 200, str(b4)[:100] if s4 != 200 else "")
    if s4 == 200:
        check("Confirmed recovered amount", b4.get("confirmed_amount") == 1800.0)
        check("Total confirmed recovered", b4.get("total_confirmed_recovered") == 1800.0)

    # Test close
    s5, b5 = post("/api/revenue-impact/" + impact_id + "/close", {
        "actor": "User",
    })
    check("Close impact record", s5 == 200, str(b5)[:100] if s5 != 200 else "")

# List impacts
s, b = get("/api/revenue-impact")
check("List revenue impacts", s == 200)
if s == 200:
    check("Has impacts list", "impacts" in b)
    check("Has totals", "totals" in b)

# ---- Phase 7: Security ----
print("\n--- Phase 7: Security & Access Governance ---")
s, b = get("/api/security/role-check")
check("Role check endpoint", s in (200, 401), str(b)[:100])

s, b = post("/api/security/authorize", {
    "action": "write_off_approval",
    "actor": "admin",
})
check("Security authorize endpoint", s in (200, 401), str(b)[:100])

# ---- Phase 8: Integrated Revenue Integrity ----
print("\n--- Phase 8: Agent & Orchestrator Integration ---")
s, b = post("/api/revenue-integrity/integrated-check")
check("Integrated revenue integrity check", s == 200, str(b)[:100] if s != 200 else "")
if s == 200:
    check("Has orchestrator_tasks_created", "orchestrator_tasks_created" in b)

# ---- Existing validation ----
print("\n--- Existing Validation Scripts ---")
s, b = get("/api/health")
check("Existing /api/health", s == 200)
s, b = get("/api/agents")
check("Existing /api/agents", s == 200)
s, b = get("/api/payments")
check("Existing /api/payments (fixed)", s == 200)

print()
print("=" * 60)
print("RESULTS: " + str(results["passed"]) + " passed, " + str(results["failed"]) + " failed")
if results["errors"]:
    print("\nErrors:")
    for e in results["errors"]:
        print("  - " + e)
print("=" * 60)

sys.exit(1 if results["failed"] > 0 else 0)