"""PNRCM Empty Tables Check.

Classifies tables into:
  * seeded        - populated by database init/demo seeding (roles, users,
                    facilities, departments, payer rules, copay rules)
  * transactional - populated only as users/agents execute workflow steps
  * missing       - table does not exist in the database

A transactional table being empty is NOT a defect until a relevant workflow has
been run. Intentionally empty tables are reported separately so they are not
treated as defects.
"""
import sqlite3, json, sys

DB = "pnrcm_demo.db"

# Tables populated by init_db seeding (present immediately after first start).
SEEDED_TABLES = {
    "user_roles", "users", "departments", "facilities",
    "payer_rules", "copayment_rules",
}

# Transactional tables populated only when specific agent actions run.
# These are expected to be empty on a fresh DB until workflows execute.
TRANSACTIONAL_TABLES = {
    "patients", "encounters", "services", "bills", "claims", "payments",
    "documentation_reviews", "claim_vetting", "denials", "appeals",
    "claim_followups", "claim_followup_attempts", "patient_balances",
    "patient_payments", "collection_tasks", "collection_attempts",
    "payment_promises", "leakage_cases", "reconciliations",
    "reconciliation_line_items", "payment_matches", "underpayment_cases",
    "recovery_workflow", "fraud_alerts", "fraud_investigations",
    "revenue_gap_cases", "write_off_audits", "recovery_cases",
    "payer_rule_versions", "leakage_memory", "exceptions", "audit_trail",
    "agent_executions", "agent_states", "agent_performance", "review_tasks",
    "orchestrator_tasks", "runs", "events", "reviews", "clinical_suggestions",
    "rcm_documents", "rcm_document_pages", "evidence_analysis_runs",
    "evidence_findings", "evidence_citations", "evidence_review_decisions",
    "evidence_feedback", "claim_chronology_events", "encounter_transitions",
    "registration_timeline", "workflow_runs", "decision_support",
    "authorizations",
    # Phase 6+ tables
    "revenue_impact_tracking", "session_access_log", "user_roles",
    "review_task_escalations", "review_assignments",
}

ALL_TABLES = SEEDED_TABLES | TRANSACTIONAL_TABLES


def check_tables():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    results = {"seeded_empty": [], "seeded_populated": [],
               "transactional_empty": [], "transactional_populated": [],
               "missing": []}
    for t in sorted(ALL_TABLES):
        try:
            row = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()
            n = row["n"]
            if t in SEEDED_TABLES:
                (results["seeded_populated"] if n > 0 else results["seeded_empty"]).append((t, n))
            else:
                (results["transactional_populated"] if n > 0 else results["transactional_empty"]).append((t, n))
        except sqlite3.OperationalError:
            results["missing"].append(t)
    conn.close()
    return results


if __name__ == "__main__":
    results = check_tables()
    print("=" * 60)
    print("PNRCM Empty Tables Check")
    print("=" * 60)

    print(f"\nSeeded tables (should be populated after init):")
    print(f"  Populated ({len(results['seeded_populated'])}):")
    for t, n in results["seeded_populated"]:
        print(f"    {t}: {n} rows")
    print(f"  Empty ({len(results['seeded_empty'])}):")
    for t, n in results["seeded_empty"]:
        print(f"    {t}: {n} rows  <-- DEFECT: seeded table not populated")

    print(f"\nTransactional tables (populated by workflow execution):")
    print(f"  Populated ({len(results['transactional_populated'])}):")
    for t, n in results["transactional_populated"]:
        print(f"    {t}: {n} rows")
    print(f"  Empty / not-yet-executed ({len(results['transactional_empty'])}):")
    for t, n in results["transactional_empty"]:
        print(f"    {t}: {n} rows  (expected until workflow runs)")

    print(f"\nMissing tables ({len(results['missing'])}):")
    for t in results["missing"]:
        print(f"    {t}  <-- DEFECT: table does not exist")

    seeded_defects = len(results["seeded_empty"]) + len(results["missing"])
    print()
    print("=" * 60)
    print(f"Seeded populated: {len(results['seeded_populated'])}")
    print(f"Seeded empty (defects): {len(results['seeded_empty'])}")
    print(f"Transactional populated: {len(results['transactional_populated'])}")
    print(f"Transactional empty (expected): {len(results['transactional_empty'])}")
    print(f"Missing (defects): {len(results['missing'])}")
    print("=" * 60)

    sys.exit(1 if seeded_defects > 0 else 0)
