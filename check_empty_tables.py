import sqlite3, json, sys

DB = "pnrcm_demo.db"

tables = [
    "patients","encounters","services","bills","claims","payments",
    "documentation_reviews","claim_vetting","denials","appeals",
    "claim_followups","claim_followup_attempts","patient_balances",
    "patient_payments","collection_tasks","collection_attempts",
    "payment_promises","leakage_cases","reconciliations",
    "reconciliation_line_items","payment_matches","underpayment_cases",
    "recovery_workflow","fraud_alerts","fraud_investigations",
    "revenue_gap_cases","write_off_audits","recovery_cases",
    "payer_rule_versions","leakage_memory","exceptions","audit_trail",
    "agent_executions","agent_states","agent_performance","review_tasks",
    "orchestrator_tasks","runs","events","reviews","clinical_suggestions",
    "rcm_documents","rcm_document_pages","evidence_analysis_runs",
    "evidence_findings","evidence_citations","evidence_review_decisions",
    "evidence_feedback","claim_chronology_events","encounter_transitions",
    "registration_timeline","workflow_runs","decision_support",
    "payer_responses","authorizations"
]

def check_tables():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    results = {"empty": [], "populated": [], "missing": []}
    for t in tables:
        try:
            row = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()
            n = row["n"]
            if n == 0:
                results["empty"].append((t, n))
            else:
                results["populated"].append((t, n))
        except sqlite3.OperationalError:
            results["missing"].append(t)
    conn.close()
    return results

if __name__ == "__main__":
    results = check_tables()
    print("=" * 60)
    print("PNRCM Empty Tables Check")
    print("=" * 60)
    print(f"\nEmpty tables ({len(results['empty'])}):")
    for t, n in results["empty"]:
        print(f"  {t}: {n} rows")
    print(f"\nPopulated tables ({len(results['populated'])}):")
    for t, n in results["populated"]:
        print(f"  {t}: {n} rows")
    print(f"\nMissing tables ({len(results['missing'])}):")
    for t in results["missing"]:
        print(f"  {t}")
    print()
    print("=" * 60)
    print(f"Empty: {len(results['empty'])}, Populated: {len(results['populated'])}, Missing: {len(results['missing'])}")
    print("=" * 60)

    sys.exit(1 if results["empty"] else 0)