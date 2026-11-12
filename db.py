"""Database schema, migrations, and utilities for PNRCM."""
import sqlite3, json, uuid, datetime, os

DB = os.environ.get("PNRCM_DB", "pnrcm_demo.db")

_wal_initialized = False

def get_db():
    global _wal_initialized
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    # journal_mode is persistent; only set once to avoid repeated lock acquisition
    if not _wal_initialized:
        try:
            c.execute("PRAGMA journal_mode=WAL")
            _wal_initialized = True
        except Exception:
            pass
    c.execute("PRAGMA busy_timeout=30000")
    return c

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

SCHEMA = [
    # Existing tables (preserved)
    """CREATE TABLE IF NOT EXISTS runs(
        id TEXT PRIMARY KEY,
        encounter_id TEXT,
        scenario TEXT,
        patient TEXT,
        status TEXT,
        total REAL,
        collected REAL,
        outstanding REAL,
        leakage REAL,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        ts TEXT,
        agent TEXT,
        event TEXT,
        status TEXT,
        payload TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS reviews(
        id TEXT PRIMARY KEY,
        run_id TEXT,
        kind TEXT,
        reason TEXT,
        amount REAL,
        status TEXT,
        created_at TEXT,
        resolved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS patients(
        id TEXT PRIMARY KEY,
        patient_id TEXT UNIQUE,
        first_name TEXT,
        last_name TEXT,
        date_of_birth TEXT,
        gender TEXT,
        phone TEXT,
        email TEXT,
        address TEXT,
        insurance_type TEXT,
        insurance_number TEXT,
        status TEXT DEFAULT 'active',
        created_at TEXT,
        updated_at TEXT,
        facility_id TEXT,
        FOREIGN KEY (facility_id) REFERENCES facilities(id)
    )""",
    """CREATE TABLE IF NOT EXISTS encounters(
        id TEXT PRIMARY KEY,
        encounter_id TEXT UNIQUE,
        patient_id TEXT,
        patient_name TEXT,
        encounter_type TEXT DEFAULT 'outpatient',
        status TEXT DEFAULT 'scheduled',
        admission_date TEXT,
        discharge_date TEXT,
        reason TEXT,
        attending_physician TEXT,
        documentation_complete INTEGER DEFAULT 0,
        facility_id TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (patient_id) REFERENCES patients(id),
        FOREIGN KEY (facility_id) REFERENCES facilities(id)
    )""",
    """CREATE TABLE IF NOT EXISTS services(
        id TEXT PRIMARY KEY,
        encounter_id TEXT,
        service_name TEXT,
        description TEXT,
        quantity INTEGER DEFAULT 1,
        unit_price REAL,
        total_price REAL,
        billed INTEGER DEFAULT 0,
        bill_id TEXT,
        created_at TEXT,
        FOREIGN KEY (encounter_id) REFERENCES encounters(id)
    )""",
    """CREATE TABLE IF NOT EXISTS bills(
        id TEXT PRIMARY KEY,
        bill_number TEXT UNIQUE,
        encounter_id TEXT,
        patient_id TEXT,
        subtotal REAL,
        tax REAL,
        discount REAL,
        total REAL,
        paid REAL DEFAULT 0,
        outstanding REAL,
        status TEXT DEFAULT 'draft',
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (encounter_id) REFERENCES encounters(id),
        FOREIGN KEY (patient_id) REFERENCES patients(id)
    )""",
    """CREATE TABLE IF NOT EXISTS bill_line_items(
        id TEXT PRIMARY KEY,
        bill_id TEXT NOT NULL,
        service_id TEXT,
        line_code TEXT,
        description TEXT,
        amount REAL NOT NULL DEFAULT 0,
        created_at TEXT,
        FOREIGN KEY (bill_id) REFERENCES bills(id),
        FOREIGN KEY (service_id) REFERENCES services(id)
    )""",
    """CREATE TABLE IF NOT EXISTS claims(
        id TEXT PRIMARY KEY,
        claim_number TEXT UNIQUE,
        encounter_id TEXT,
        patient_id TEXT,
        bill_id TEXT,
        payer_type TEXT,
        payer_name TEXT,
        claim_type TEXT DEFAULT 'professional',
        diagnosis_codes TEXT,
        procedure_codes TEXT,
        total_charges REAL,
        approved_amount REAL DEFAULT 0,
        paid_amount REAL DEFAULT 0,
        status TEXT DEFAULT 'draft',
        denial_reason TEXT,
        denial_category TEXT,
        submitted_at TEXT,
        paid_at TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (encounter_id) REFERENCES encounters(id),
        FOREIGN KEY (patient_id) REFERENCES patients(id),
        FOREIGN KEY (bill_id) REFERENCES bills(id)
    )""",
    """CREATE TABLE IF NOT EXISTS payments(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        bill_id TEXT,
        patient_id TEXT,
        amount REAL,
        payment_method TEXT,
        payment_date TEXT,
        status TEXT DEFAULT 'pending',
        reference TEXT,
        created_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id),
        FOREIGN KEY (bill_id) REFERENCES bills(id),
        FOREIGN KEY (patient_id) REFERENCES patients(id)
    )""",
    """CREATE TABLE IF NOT EXISTS denials(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        denial_date TEXT,
        denial_reason TEXT,
        denial_category TEXT,
        corrective_action TEXT,
        status TEXT DEFAULT 'open',
        resubmission_count INTEGER DEFAULT 0,
        resolved_at TEXT,
        created_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id)
    )""",
    """CREATE TABLE IF NOT EXISTS documentation_reviews(
        id TEXT PRIMARY KEY,
        encounter_id TEXT,
        review_type TEXT DEFAULT 'clinical',
        findings TEXT,
        status TEXT DEFAULT 'pending',
        human_review_required INTEGER DEFAULT 1,
        reviewer_decision TEXT,
        reviewer_notes TEXT,
        resolved_at TEXT,
        created_at TEXT,
        FOREIGN KEY (encounter_id) REFERENCES encounters(id)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_executions(
        id TEXT PRIMARY KEY,
        agent_name TEXT,
        task_name TEXT,
        entity_type TEXT,
        entity_id TEXT,
        status TEXT DEFAULT 'pending',
        started_at TEXT,
        completed_at TEXT,
        duration_ms INTEGER,
        input_data TEXT,
        output_data TEXT,
        error_message TEXT,
        human_review_required INTEGER DEFAULT 0,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS audit_trail(
        id TEXT PRIMARY KEY,
        timestamp TEXT,
        actor TEXT,
        actor_type TEXT,
        agent_name TEXT,
        action TEXT,
        entity_type TEXT,
        entity_id TEXT,
        previous_state TEXT,
        new_state TEXT,
        recommendation TEXT,
        decision TEXT,
        execution_duration_ms INTEGER,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS payer_rules(
        id TEXT PRIMARY KEY,
        rule_name TEXT,
        rule_type TEXT,
        description TEXT,
        condition TEXT,
        severity TEXT DEFAULT 'medium',
        is_active INTEGER DEFAULT 1,
        is_simulated INTEGER DEFAULT 1,
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS claim_vetting(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        vetting_status TEXT,
        issues TEXT,
        created_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id)
    )""",
    # New extension tables
    """CREATE TABLE IF NOT EXISTS encounter_transitions(
        id TEXT PRIMARY KEY,
        encounter_id TEXT,
        from_status TEXT,
        to_status TEXT,
        ts TEXT,
        actor TEXT,
        note TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS registration_timeline(
        id TEXT PRIMARY KEY,
        patient_id TEXT,
        ts TEXT,
        agent_name TEXT,
        action TEXT,
        detail TEXT,
        status TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS clinical_suggestions(
        id TEXT PRIMARY KEY,
        encounter_id TEXT,
        field TEXT,
        original_value TEXT,
        suggested_value TEXT,
        explanation TEXT,
        confidence REAL,
        status TEXT DEFAULT 'pending_review',
        reviewer TEXT,
        reviewer_decision TEXT,
        reviewer_notes TEXT,
        created_at TEXT,
        resolved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_runs(
        id TEXT PRIMARY KEY,
        workflow_type TEXT,
        entity_type TEXT,
        entity_id TEXT,
        status TEXT,
        started_at TEXT,
        completed_at TEXT,
        summary TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS exceptions(
        id TEXT PRIMARY KEY,
        category TEXT,
        severity TEXT,
        title TEXT,
        explanation TEXT,
        recommended_action TEXT,
        status TEXT DEFAULT 'open',
        patient_id TEXT,
        encounter_id TEXT,
        claim_id TEXT,
        bill_id TEXT,
        detecting_agent TEXT,
        assigned_reviewer TEXT,
        resolution_notes TEXT,
        created_at TEXT,
        updated_at TEXT,
        resolved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS decision_support(
        id TEXT PRIMARY KEY,
        entity_type TEXT,
        entity_id TEXT,
        recommendation TEXT,
        reasoning TEXT,
        confidence REAL,
        uncertainty TEXT,
        alternatives TEXT,
        estimated_impact REAL,
        status TEXT DEFAULT 'pending',
        human_decision TEXT,
        human_notes TEXT,
        created_at TEXT,
        resolved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS leakage_cases(
        id TEXT PRIMARY KEY,
        facility_id TEXT,
        category TEXT,
        description TEXT,
        detection_source TEXT,
        detection_rule TEXT,
        supporting_evidence TEXT,
        estimated_exposure REAL,
        confirmed_amount REAL,
        verified_recovered_amount REAL,
        priority TEXT,
        status TEXT DEFAULT 'DETECTED',
        assigned_user TEXT,
        recommended_action TEXT,
        resolution_notes TEXT,
        false_positive_reason TEXT,
        entity_type TEXT,
        entity_id TEXT,
        patient_id TEXT,
        encounter_id TEXT,
        claim_id TEXT,
        created_at TEXT,
        updated_at TEXT,
        resolved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS appeals(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        denial_category TEXT,
        status TEXT DEFAULT 'DETECTED',
        payer_name TEXT,
        denial_reason TEXT,
        appeal_letter TEXT,
        supporting_docs TEXT,
        amount_under_appeal REAL,
        assigned_staff TEXT,
        deadline TEXT,
        created_at TEXT,
        updated_at TEXT,
        resolved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS claim_followups(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        status TEXT DEFAULT 'AWAITING_PAYER_RESPONSE',
        priority TEXT DEFAULT 'medium',
        assigned_staff TEXT,
        due_date TEXT,
        last_contact_date TEXT,
        next_followup_date TEXT,
        notes TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS claim_followup_attempts(
        id TEXT PRIMARY KEY,
        followup_id TEXT,
        ts TEXT,
        method TEXT,
        note TEXT,
        outcome TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS patient_balances(
        id TEXT PRIMARY KEY,
        patient_id TEXT,
        encounter_id TEXT,
        expected_copayment REAL DEFAULT 0,
        amount_due REAL DEFAULT 0,
        amount_paid REAL DEFAULT 0,
        outstanding REAL DEFAULT 0,
        status TEXT DEFAULT 'COPAYMENT_PENDING',
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS patient_payments(
        id TEXT PRIMARY KEY,
        balance_id TEXT,
        patient_id TEXT,
        encounter_id TEXT,
        amount REAL,
        payment_method TEXT,
        payment_date TEXT,
        payment_status TEXT DEFAULT 'pending',
        reference TEXT,
        recorded_by TEXT,
        reconciliation_status TEXT DEFAULT 'unreconciled',
        reversal_of TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS collection_tasks(
        id TEXT PRIMARY KEY,
        balance_id TEXT,
        patient_id TEXT,
        encounter_id TEXT,
        status TEXT DEFAULT 'open',
        priority TEXT DEFAULT 'medium',
        assigned_staff TEXT,
        due_date TEXT,
        note TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS collection_attempts(
        id TEXT PRIMARY KEY,
        task_id TEXT,
        ts TEXT,
        channel TEXT,
        template TEXT,
        outcome TEXT,
        note TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS payment_promises(
        id TEXT PRIMARY KEY,
        balance_id TEXT,
        promised_amount REAL,
        promised_date TEXT,
        status TEXT DEFAULT 'open',
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS copayment_rules(
        id TEXT PRIMARY KEY,
        payer_type TEXT,
        encounter_type TEXT,
        amount REAL,
        description TEXT,
        is_active INTEGER DEFAULT 1,
        is_simulated INTEGER DEFAULT 1,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS fraud_alerts(
        id TEXT PRIMARY KEY,
        detection_date TEXT,
        risk_score INTEGER,
        risk_level TEXT,
        anomaly_category TEXT,
        related_claim_id TEXT,
        related_encounter_id TEXT,
        related_bill_id TEXT,
        related_payment_id TEXT,
        provider_or_department TEXT,
        estimated_exposure REAL,
        detection_rule TEXT,
        evidence TEXT,
        explanation TEXT,
        recommended_action TEXT,
        status TEXT DEFAULT 'NEW',
        assigned_investigator TEXT,
        resolution_notes TEXT,
        created_at TEXT,
        updated_at TEXT,
        resolved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS fraud_investigations(
        id TEXT PRIMARY KEY,
        alert_id TEXT,
        status TEXT DEFAULT 'NEW',
        assigned_investigator TEXT,
        notes TEXT,
        human_decision TEXT,
        confirmed_discrepancy REAL,
        amount_corrected REAL,
        amount_recovered REAL,
        created_at TEXT,
        updated_at TEXT,
        resolved_at TEXT
    )""",
    # RCM Evidence Intelligence
    """CREATE TABLE IF NOT EXISTS rcm_documents(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        encounter_id TEXT,
        filename TEXT,
        doc_type TEXT,
        page_count INTEGER DEFAULT 1,
        content_text TEXT,
        source_path TEXT,
        metadata TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rcm_document_pages(
        id TEXT PRIMARY KEY,
        document_id TEXT,
        page_number INTEGER,
        text TEXT,
        created_at TEXT,
        FOREIGN KEY (document_id) REFERENCES rcm_documents(id)
    )""",
    """CREATE TABLE IF NOT EXISTS evidence_analysis_runs(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        status TEXT DEFAULT 'pending',
        method TEXT,
        summary TEXT,
        started_at TEXT,
        completed_at TEXT,
        duration_ms INTEGER,
        error_message TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS evidence_findings(
        id TEXT PRIMARY KEY,
        run_id TEXT,
        claim_id TEXT,
        category TEXT,
        description TEXT,
        severity TEXT,
        why_it_matters TEXT,
        recommended_action TEXT,
        confidence TEXT,
        interpretation_type TEXT,
        review_status TEXT DEFAULT 'Pending Review',
        relevant_claim_field TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS evidence_citations(
        id TEXT PRIMARY KEY,
        finding_id TEXT,
        document_id TEXT,
        document_filename TEXT,
        page_number INTEGER,
        relevant_text TEXT,
        claim_field_or_line TEXT,
        citation_status TEXT DEFAULT 'verified',
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS claim_chronology_events(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        event_type TEXT,
        event_time TEXT,
        event_source TEXT,
        related_reference TEXT,
        details TEXT,
        is_unknown INTEGER DEFAULT 0,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS evidence_review_decisions(
        id TEXT PRIMARY KEY,
        finding_id TEXT,
        claim_id TEXT,
        reviewer TEXT,
        decision TEXT,
        corrected_description TEXT,
        notes TEXT,
        evidence_relevance TEXT,
        missing_evidence_note TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS evidence_feedback(
        id TEXT PRIMARY KEY,
        finding_id TEXT,
        claim_id TEXT,
        reviewer TEXT,
        action TEXT,
        reason TEXT,
        corrected_description TEXT,
        marked_irrelevant INTEGER DEFAULT 0,
        missing_evidence TEXT,
        original_description TEXT,
        created_at TEXT
    )""",
    # Reconciliation & Verified Revenue Recovery
    """CREATE TABLE IF NOT EXISTS reconciliations(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        claim_number TEXT,
        patient_id TEXT,
        payer_name TEXT,
        payer_type TEXT,
        billed_amount REAL DEFAULT 0,
        approved_amount REAL DEFAULT 0,
        expected_reimbursement REAL DEFAULT 0,
        adjustments_total REAL DEFAULT 0,
        total_payments_received REAL DEFAULT 0,
        reconciled_payments REAL DEFAULT 0,
        outstanding_balance REAL DEFAULT 0,
        variance REAL DEFAULT 0,
        status TEXT DEFAULT 'Awaiting Adjudication',
        reconciliation_date TEXT,
        reviewed_by TEXT,
        review_decision TEXT DEFAULT 'pending',
        review_notes TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id)
    )""",
    # Revenue Integrity Intelligence (embedded, no standalone workspace)
    """CREATE TABLE IF NOT EXISTS payer_rule_versions(
        id TEXT PRIMARY KEY,
        rule_id TEXT,
        rule_name TEXT,
        rule_type TEXT,
        version INTEGER DEFAULT 1,
        condition_snapshot TEXT,
        severity TEXT,
        is_active INTEGER DEFAULT 1,
        changed_by TEXT,
        change_reason TEXT,
        effective_date TEXT,
        created_at TEXT,
        FOREIGN KEY (rule_id) REFERENCES payer_rules(id)
    )""",
    """CREATE TABLE IF NOT EXISTS write_off_audits(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        claim_number TEXT,
        encounter_id TEXT,
        amount REAL DEFAULT 0,
        adjustment_type TEXT,
        reason TEXT,
        status TEXT DEFAULT 'pending_review',
        flagged_reasons TEXT,
        recoverable_amount REAL DEFAULT 0,
        reviewer TEXT,
        review_decision TEXT DEFAULT 'pending',
        review_notes TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id)
    )""",
    """CREATE TABLE IF NOT EXISTS revenue_gap_cases(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        encounter_id TEXT,
        patient_id TEXT,
        gap_type TEXT,
        description TEXT,
        amount_involved REAL DEFAULT 0,
        potential_recoverable REAL DEFAULT 0,
        status TEXT DEFAULT 'Detected',
        responsible_workflow TEXT,
        evidence TEXT,
        created_at TEXT,
        updated_at TEXT,
        resolved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS leakage_memory(
        id TEXT PRIMARY KEY,
        pattern_type TEXT,
        pattern_key TEXT,
        occurrence_count INTEGER DEFAULT 1,
        first_seen TEXT,
        last_seen TEXT,
        total_amount REAL DEFAULT 0,
        confirmed_recoverable REAL DEFAULT 0,
        description TEXT,
        status TEXT DEFAULT 'active'
    )""",
    """CREATE TABLE IF NOT EXISTS recovery_cases(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        underpayment_case_id TEXT,
        discrepancy_type TEXT,
        status TEXT DEFAULT 'Detected',
        priority_score REAL DEFAULT 0,
        priority_reason TEXT,
        expected_amount REAL DEFAULT 0,
        recovered_amount REAL DEFAULT 0,
        verified_amount REAL DEFAULT 0,
        workflow_stage TEXT DEFAULT 'case_created',
        assigned_workflow TEXT,
        created_at TEXT,
        updated_at TEXT,
        resolved_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id)
    )""",
    """CREATE TABLE IF NOT EXISTS reconciliation_line_items(
        id TEXT PRIMARY KEY,
        reconciliation_id TEXT,
        line_type TEXT,
        description TEXT,
        amount REAL DEFAULT 0,
        reference_id TEXT,
        source_table TEXT,
        source_id TEXT,
        created_at TEXT,
        FOREIGN KEY (reconciliation_id) REFERENCES reconciliations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS payment_matches(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        payment_id TEXT,
        matched_amount REAL DEFAULT 0,
        match_status TEXT DEFAULT 'unmatched',
        matched_at TEXT,
        matched_by TEXT,
        notes TEXT,
        reconciliation_status TEXT DEFAULT 'unreconciled',
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id),
        FOREIGN KEY (payment_id) REFERENCES payments(id)
    )""",
    """CREATE TABLE IF NOT EXISTS underpayment_cases(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        claim_number TEXT,
        payer_name TEXT,
        expected_reimbursement REAL DEFAULT 0,
        reconciled_payment REAL DEFAULT 0,
        outstanding_amount REAL DEFAULT 0,
        discrepancy_reason TEXT,
        status TEXT DEFAULT 'open',
        recommended_action TEXT,
        followup_id TEXT,
        appeal_id TEXT,
        created_at TEXT,
        updated_at TEXT,
        resolved_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id)
    )""",
    """CREATE TABLE IF NOT EXISTS recovery_workflow(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        underpayment_case_id TEXT,
        workflow_stage TEXT,
        stage_status TEXT DEFAULT 'pending',
        actor TEXT,
        notes TEXT,
        financial_value REAL DEFAULT 0,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id),
        FOREIGN KEY (underpayment_case_id) REFERENCES underpayment_cases(id)
    )""",
    # Revenue OS workspace tables
    """CREATE TABLE IF NOT EXISTS departments(
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        code TEXT,
        description TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS users(
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        email TEXT,
        role TEXT DEFAULT 'department_staff',
        is_active INTEGER DEFAULT 1,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS review_tasks(
        id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        priority TEXT DEFAULT 'medium',
        assigned_to TEXT,
        context TEXT,
        evidence TEXT,
        ai_recommendation TEXT,
        financial_impact REAL DEFAULT 0,
        created_at TEXT,
        resolved_at TEXT,
        FOREIGN KEY (assigned_to) REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS orchestrator_tasks(
        id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        status TEXT DEFAULT 'created',
        priority TEXT DEFAULT 'medium',
        assigned_to TEXT,
        assigned_by TEXT,
        agent_name TEXT,
        direction TEXT DEFAULT 'agent_to_human',
        context TEXT,
        evidence TEXT,
        ai_recommendation TEXT,
        financial_impact REAL DEFAULT 0,
        started_at TEXT,
        completed_at TEXT,
        created_at TEXT,
        resolved_at TEXT,
        FOREIGN KEY (assigned_to) REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_states(
        agent_name TEXT PRIMARY KEY,
        role TEXT,
        status TEXT DEFAULT 'Idle',
        current_task TEXT,
        started_at TEXT,
        completed_at TEXT,
        duration_ms INTEGER,
        last_activity TEXT,
        tasks_completed INTEGER DEFAULT 0,
        exceptions INTEGER DEFAULT 0,
        output TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS agent_performance(
        id TEXT PRIMARY KEY,
        agent_name TEXT NOT NULL,
        task_type TEXT,
        status TEXT,
        started_at TEXT,
        completed_at TEXT,
        duration_ms INTEGER,
        human_review_required INTEGER DEFAULT 0,
        error_message TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS charge_line_items(
        id TEXT PRIMARY KEY,
        claim_id TEXT,
        bill_id TEXT,
        service_id TEXT,
        line_code TEXT,
        description TEXT,
        amount REAL DEFAULT 0,
        created_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id),
        FOREIGN KEY (bill_id) REFERENCES bills(id),
        FOREIGN KEY (service_id) REFERENCES services(id)
    )""",
    """CREATE TABLE IF NOT EXISTS payment_allocations(
        id TEXT PRIMARY KEY,
        payment_id TEXT,
        claim_id TEXT,
        bill_id TEXT,
        amount REAL DEFAULT 0,
        allocated_at TEXT,
        allocated_by TEXT,
        FOREIGN KEY (payment_id) REFERENCES payments(id),
        FOREIGN KEY (claim_id) REFERENCES claims(id),
        FOREIGN KEY (bill_id) REFERENCES bills(id)
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_steps(
        id TEXT PRIMARY KEY,
        encounter_id TEXT,
        step_name TEXT NOT NULL,
        step_order INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending',
        started_at TEXT,
        completed_at TEXT,
        actor TEXT,
        note TEXT,
        created_at TEXT,
        FOREIGN KEY (encounter_id) REFERENCES encounters(id)
    )""",
    # Revenue OS extension tables
    """CREATE TABLE IF NOT EXISTS facilities(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        region TEXT,
        address TEXT,
        phone TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS authorizations(
        id TEXT PRIMARY KEY,
        auth_number TEXT UNIQUE NOT NULL,
        patient_id TEXT,
        encounter_id TEXT,
        payer_name TEXT,
        payer_type TEXT,
        procedure_code TEXT,
        service_description TEXT,
        status TEXT DEFAULT 'pending',
        requested_at TEXT,
        approved_at TEXT,
        expiry_date TEXT,
        required_documents TEXT,
        submitted_documents TEXT,
        approving_staff TEXT,
        rejection_reason TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (patient_id) REFERENCES patients(id),
        FOREIGN KEY (encounter_id) REFERENCES encounters(id)
    )""",
    """CREATE TABLE IF NOT EXISTS coding_suggestions(
        id TEXT PRIMARY KEY,
        encounter_id TEXT,
        claim_id TEXT,
        code_type TEXT,
        code_value TEXT,
        code_description TEXT,
        confidence REAL,
        evidence TEXT,
        status TEXT DEFAULT 'pending_review',
        reviewer TEXT,
        reviewer_decision TEXT,
        reviewer_notes TEXT,
        created_at TEXT,
        resolved_at TEXT,
        FOREIGN KEY (encounter_id) REFERENCES encounters(id),
        FOREIGN KEY (claim_id) REFERENCES claims(id)
    )""",
    """CREATE TABLE IF NOT EXISTS claim_states(
        id TEXT PRIMARY KEY,
        claim_id TEXT UNIQUE NOT NULL,
        current_state TEXT DEFAULT 'draft',
        previous_state TEXT,
        state_reason TEXT,
        transitioned_by TEXT,
        transitioned_at TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id)
    )""",
    """CREATE TABLE IF NOT EXISTS demo_scenarios(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        scenario_type TEXT,
        status TEXT DEFAULT 'ready',
        current_step INTEGER DEFAULT 0,
        total_steps INTEGER DEFAULT 0,
        data_seed TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS demo_scenario_steps(
        id TEXT PRIMARY KEY,
        scenario_id TEXT,
        step_order INTEGER DEFAULT 0,
        step_name TEXT,
        step_description TEXT,
        status TEXT DEFAULT 'pending',
        completed_at TEXT,
        evidence TEXT,
        screen TEXT,
        created_at TEXT,
        FOREIGN KEY (scenario_id) REFERENCES demo_scenarios(id)
    )""",
    # CLAIM-it Integration tables (demonstration only - not a live CLAIM-it connector)
    """CREATE TABLE IF NOT EXISTS claimit_integration_config(
        id TEXT PRIMARY KEY,
        facility_id TEXT,
        integration_mode TEXT DEFAULT 'file_based',
        status TEXT DEFAULT 'demo',
        last_sync TEXT,
        pending_transfers INTEGER DEFAULT 0,
        pending_feedback INTEGER DEFAULT 0,
        failed_operations INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (facility_id) REFERENCES facilities(id)
    )""",
    """CREATE TABLE IF NOT EXISTS claimit_exchange_queue(
        id TEXT PRIMARY KEY,
        claim_ref TEXT UNIQUE NOT NULL,
        claim_id TEXT,
        patient_id TEXT,
        encounter_id TEXT,
        facility_id TEXT,
        payer_name TEXT,
        claim_amount REAL DEFAULT 0,
        pnrcm_status TEXT DEFAULT 'draft',
        claimit_status TEXT DEFAULT 'not_exported',
        validation_result TEXT,
        last_action TEXT,
        next_action TEXT,
        export_file TEXT,
        import_file TEXT,
        audit_trail TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id),
        FOREIGN KEY (encounter_id) REFERENCES encounters(id),
        FOREIGN KEY (facility_id) REFERENCES facilities(id)
    )""",
    """CREATE TABLE IF NOT EXISTS claimit_validation_issues(
        id TEXT PRIMARY KEY,
        claim_ref TEXT,
        issue_type TEXT,
        severity TEXT,
        description TEXT,
        source_record TEXT,
        related_claim_id TEXT,
        related_encounter_id TEXT,
        evidence TEXT,
        recommended_action TEXT,
        responsible_role TEXT,
        status TEXT DEFAULT 'open',
        created_at TEXT,
        resolved_at TEXT,
        FOREIGN KEY (related_claim_id) REFERENCES claims(id),
        FOREIGN KEY (related_encounter_id) REFERENCES encounters(id)
    )""",
    """CREATE TABLE IF NOT EXISTS claimit_file_exchange(
        id TEXT PRIMARY KEY,
        file_name TEXT NOT NULL,
        file_type TEXT,
        facility_id TEXT,
        claim_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'prepared',
        created_at TEXT,
        transferred_at TEXT,
        imported_at TEXT,
        validation_summary TEXT,
        error_summary TEXT,
        audit_trail TEXT,
        FOREIGN KEY (facility_id) REFERENCES facilities(id)
    )""",
    """CREATE TABLE IF NOT EXISTS claimit_feedback(
        id TEXT PRIMARY KEY,
        feedback_ref TEXT UNIQUE NOT NULL,
        claim_ref TEXT,
        claim_id TEXT,
        facility_id TEXT,
        feedback_type TEXT,
        outcome TEXT,
        details TEXT,
        processed INTEGER DEFAULT 0,
        processed_at TEXT,
        created_at TEXT,
        FOREIGN KEY (claim_id) REFERENCES claims(id),
        FOREIGN KEY (facility_id) REFERENCES facilities(id)
    )""",
    """CREATE TABLE IF NOT EXISTS claimit_activity_log(
        id TEXT PRIMARY KEY,
        claim_ref TEXT,
        facility_id TEXT,
        actor TEXT,
        actor_type TEXT,
        action TEXT,
        previous_status TEXT,
        new_status TEXT,
        timestamp TEXT,
        evidence TEXT,
        FOREIGN KEY (facility_id) REFERENCES facilities(id)
    )""",
    # ---------------- Revenue Impact Tracking (Phase 6) ----------------
    """CREATE TABLE IF NOT EXISTS revenue_impact_tracking(
        id TEXT PRIMARY KEY,
        impact_id TEXT UNIQUE NOT NULL,
        source_finding_id TEXT,
        source_finding_type TEXT,
        related_claim_id TEXT,
        related_encounter_id TEXT,
        related_charge_id TEXT,
        related_payment_id TEXT,
        original_issue TEXT,
        lifecycle_stage TEXT DEFAULT 'IDENTIFIED',
        initial_estimated_amount REAL DEFAULT 0,
        confirmed_recovered_amount REAL DEFAULT 0,
        confirmed_protected_amount REAL DEFAULT 0,
        outcome_category TEXT,
        assigned_owner TEXT,
        human_verification_status TEXT DEFAULT 'unverified',
        human_verifier TEXT,
        verification_date TEXT,
        resolution_date TEXT,
        supporting_evidence TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (related_claim_id) REFERENCES claims(id),
        FOREIGN KEY (related_encounter_id) REFERENCES encounters(id),
        FOREIGN KEY (related_payment_id) REFERENCES payments(id)
    )""",
    # ---------------- Security & Access Governance (Phase 7) ----------------
    """CREATE TABLE IF NOT EXISTS session_access_log(
        id TEXT PRIMARY KEY,
        session_id TEXT,
        user_id TEXT,
        username TEXT,
        role TEXT,
        facility_id TEXT,
        action TEXT,
        resource TEXT,
        resource_id TEXT,
        ip_address TEXT,
        user_agent TEXT,
        status TEXT,
        timestamp TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS user_roles(
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        role TEXT NOT NULL,
        facility_id TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT,
        updated_at TEXT
    )""",
    # ---------------- Review Enhancements (Phase 5) ----------------
    """CREATE TABLE IF NOT EXISTS review_task_escalations(
        id TEXT PRIMARY KEY,
        review_task_id TEXT,
        escalated_from TEXT,
        escalated_to TEXT,
        reason TEXT,
        escalated_at TEXT,
        resolved_at TEXT,
        FOREIGN KEY (review_task_id) REFERENCES review_tasks(id)
    )""",
    """CREATE TABLE IF NOT EXISTS review_assignments(
        id TEXT PRIMARY KEY,
        review_task_id TEXT,
        assigned_to TEXT,
        assigned_by TEXT,
        assigned_at TEXT,
        unassigned_at TEXT,
        FOREIGN KEY (review_task_id) REFERENCES review_tasks(id)
    )""",
]

DEFAULT_RULES = [
    {"rule_name":"Required claim fields present","rule_type":"required_field","description":"Claim must include patient, encounter, diagnosis codes, procedure codes, and charges.","condition":json.dumps({"fields":["patient_id","encounter_id","diagnosis_codes","procedure_codes","total_charges"]}),"severity":"critical","is_simulated":1},
    {"rule_name":"Patient-encounter identity consistency","rule_type":"consistency","description":"Claim patient must match the patient linked to the encounter.","condition":json.dumps({"check":"patient_encounter_match"}),"severity":"high","is_simulated":1},
    {"rule_name":"Encounter completeness","rule_type":"consistency","description":"Encounter must be completed before claim submission.","condition":json.dumps({"check":"encounter_completed"}),"severity":"high","is_simulated":1},
    {"rule_name":"Diagnosis-procedure consistency","rule_type":"consistency","description":"Procedure codes must be consistent with diagnosis codes (simulated).","condition":json.dumps({"check":"diagnosis_procedure_match"}),"severity":"medium","is_simulated":1},
    {"rule_name":"Duplicate claim detection","rule_type":"duplicate","description":"No duplicate claim for the same encounter and payer.","condition":json.dumps({"check":"duplicate_claim"}),"severity":"high","is_simulated":1},
    {"rule_name":"Missing charges check","rule_type":"value_range","description":"All delivered services should be billed.","condition":json.dumps({"check":"no_unbilled_services"}),"severity":"medium","is_simulated":1},
    {"rule_name":"Valid billing values","rule_type":"value_range","description":"Charges and totals must be positive and consistent.","condition":json.dumps({"check":"valid_totals"}),"severity":"medium","is_simulated":1},
    {"rule_name":"Authorization required (simulated)","rule_type":"required_field","description":"Configured payers may require authorization reference (simulated rule).","condition":json.dumps({"fields":["payer_name"]}),"severity":"medium","is_simulated":1},
]

DEFAULT_COPAY_RULES = [
    {"payer_type":"nhis","encounter_type":"outpatient","amount":15.0,"description":"Simulated NHIS outpatient copay (GHS)"},
    {"payer_type":"private","encounter_type":"outpatient","amount":50.0,"description":"Simulated private outpatient copay (GHS)"},
    {"payer_type":"self_pay","encounter_type":"outpatient","amount":100.0,"description":"Simulated self-pay deposit/copay (GHS)"},
]

def _ensure_col(c, table, col, decl):
    cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

def init_db():
    c = get_db()
    for stmt in SCHEMA:
        c.execute(stmt)
    # Safe migrations for existing DBs
    _ensure_col(c, "denials", "created_at", "TEXT")
    _ensure_col(c, "patients", "facility_id", "TEXT")
    _ensure_col(c, "patients", "financial_profile", "TEXT")
    _ensure_col(c, "patients", "duplicate_flags", "TEXT")
    _ensure_col(c, "patients", "registration_notes", "TEXT")
    _ensure_col(c, "encounters", "department", "TEXT")
    _ensure_col(c, "claims", "expected_reimbursement", "REAL")
    _ensure_col(c, "claims", "authorization_ref", "TEXT")
    _ensure_col(c, "bills", "review_status", "TEXT")
    _ensure_col(c, "audit_trail", "workflow_id", "TEXT")
    _ensure_col(c, "audit_trail", "input_summary", "TEXT")
    _ensure_col(c, "audit_trail", "output_summary", "TEXT")
    _ensure_col(c, "audit_trail", "exception", "TEXT")
    _ensure_col(c, "audit_trail", "human_reviewer", "TEXT")
    _ensure_col(c, "audit_trail", "review_decision", "TEXT")
    _ensure_col(c, "agent_executions", "workflow_id", "TEXT")
    _ensure_col(c, "agent_executions", "orchestrator_action", "TEXT")
    _ensure_col(c, "rcm_document_pages", "created_at", "TEXT")
    _ensure_col(c, "write_off_audits", "updated_at", "TEXT")
    # Leakage case schema enhancements (Revenue Leakage Detection & Resolution)
    _ensure_col(c, "leakage_cases", "facility_id", "TEXT")
    _ensure_col(c, "leakage_cases", "description", "TEXT")
    _ensure_col(c, "leakage_cases", "detection_source", "TEXT")
    _ensure_col(c, "leakage_cases", "detection_rule", "TEXT")
    _ensure_col(c, "leakage_cases", "supporting_evidence", "TEXT")
    _ensure_col(c, "leakage_cases", "estimated_exposure", "REAL")
    _ensure_col(c, "leakage_cases", "confirmed_amount", "REAL")
    _ensure_col(c, "leakage_cases", "verified_recovered_amount", "REAL")
    _ensure_col(c, "leakage_cases", "priority", "TEXT")
    _ensure_col(c, "leakage_cases", "assigned_user", "TEXT")
    _ensure_col(c, "leakage_cases", "resolution_notes", "TEXT")
    _ensure_col(c, "leakage_cases", "false_positive_reason", "TEXT")
    _ensure_col(c, "leakage_cases", "detection_date", "TEXT")
    # Review task enhancements (Phase 5)
    _ensure_col(c, "review_tasks", "due_date", "TEXT")
    _ensure_col(c, "review_tasks", "priority", "TEXT")
    _ensure_col(c, "review_tasks", "reviewer", "TEXT")
    _ensure_col(c, "review_tasks", "human_decision", "TEXT")
    _ensure_col(c, "review_tasks", "decision_timestamp", "TEXT")
    _ensure_col(c, "review_tasks", "ai_recommendation", "TEXT")
    _ensure_col(c, "review_tasks", "financial_impact", "REAL")
    # Seed default user roles (Phase 7)
    if c.execute("SELECT COUNT(*) n FROM user_roles").fetchone()["n"] == 0:
        default_roles = [
            ("admin", "administrator", None),
            ("rev_mgr", "revenue_cycle_manager", None),
            ("bill_off", "billing_officer", None),
            ("claims_off", "claims_officer", None),
            ("fin_off", "finance_officer", None),
            ("doc_reviewer", "clinical_documentation_reviewer", None),
            ("dept_staff", "department_staff", None),
        ]
        for uname, role, fac_id in default_roles:
            rid = str(uuid.uuid4())
            c.execute(
                """INSERT INTO user_roles(id, username, role, facility_id, is_active, created_at, updated_at)
                VALUES(?,?,?,?,1,?,?)""",
                (rid, uname, role, fac_id, now(), now()),
            )
    # Seed payer rules
    if c.execute("SELECT COUNT(*) n FROM payer_rules").fetchone()["n"] == 0:
        for r in DEFAULT_RULES:
            c.execute(
                "INSERT INTO payer_rules(id, rule_name, rule_type, description, condition, severity, is_active, is_simulated, created_at, updated_at) VALUES(?,?,?,?,?,?,1,?,?,?)",
                (str(uuid.uuid4()), r["rule_name"], r["rule_type"], r["description"], r["condition"], r["severity"], r["is_simulated"], now(), now())
            )
    # Seed copay rules
    if c.execute("SELECT COUNT(*) n FROM copayment_rules").fetchone()["n"] == 0:
        for r in DEFAULT_COPAY_RULES:
            c.execute(
                "INSERT INTO copayment_rules(id,payer_type,encounter_type,amount,description,is_active,is_simulated,created_at) VALUES(?,?,?,?,?,1,1,?)",
                (str(uuid.uuid4()), r["payer_type"], r["encounter_type"], r["amount"], r["description"], now())
            )
    # Seed departments
    if c.execute("SELECT COUNT(*) n FROM departments").fetchone()["n"] == 0:
        depts = [
            ("Emergency", "ER", "Emergency department"),
            ("Internal Medicine", "IM", "Internal medicine department"),
            ("Pediatrics", "PED", "Pediatrics department"),
            ("Surgery", "SUR", "Surgical department"),
            ("Laboratory", "LAB", "Clinical laboratory"),
            ("Pharmacy", "PHARM", "Pharmacy department"),
            ("Radiology", "RAD", "Radiology and imaging"),
            ("Cardiology", "CAR", "Cardiology department"),
            ("Obstetrics", "OBS", "Obstetrics and gynecology"),
            ("Billing", "BILL", "Billing and revenue cycle"),
        ]
        for name, code, desc in depts:
            c.execute(
                "INSERT INTO departments(id, name, code, description, created_at) VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), name, code, desc, now())
            )
    # Seed users
    if c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0:
        users = [
            ("admin", "Admin User", "administrator"),
            ("rev_mgr", "Revenue Cycle Manager", "revenue_cycle_manager"),
            ("bill_off", "Billing Officer", "billing_officer"),
            ("claims_off", "Claims Officer", "claims_officer"),
            ("fin_off", "Finance Officer", "finance_officer"),
            ("doc_reviewer", "Clinical Documentation Reviewer", "clinical_documentation_reviewer"),
            ("dept_staff", "Department Staff", "department_staff"),
        ]
        for uname, full, role in users:
            c.execute(
                "INSERT INTO users(id, username, email, role, created_at) VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), uname, f"{uname}@pnrcm.demo", role, now())
            )
    # Seed facilities
    if c.execute("SELECT COUNT(*) n FROM facilities").fetchone()["n"] == 0:
        facilities = [
            ("Greater Accra Regional Hospital", "GARH", "Greater Accra", "Accra, Ghana", "+233 30 2xx xxxx"),
            ("Komfo Anokye Teaching Hospital", "KATH", "Ashanti", "Kumasi, Ghana", "+233 51 2xx xxxx"),
            ("Cape Coast Teaching Hospital", "CCTH", "Central", "Cape Coast, Ghana", "+233 42 2xx xxxx"),
            ("Tamale Teaching Hospital", "TTH", "Northern", "Tamale, Ghana", "+233 71 2xx xxxx"),
        ]
        for name, code, region, address, phone in facilities:
            c.execute(
                "INSERT INTO facilities(id, name, code, region, address, phone, created_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), name, code, region, address, phone, now())
            )
    # Seed demo scenarios
    if c.execute("SELECT COUNT(*) n FROM demo_scenarios").fetchone()["n"] == 0:
        scenarios = [
            ("Clean Claim Journey", "End-to-end: registration to payment for a fully covered NHIS encounter.", "clean_claim", 10),
            ("Rejected Claim Recovery", "Pre-submission validation catches missing documentation, claim resubmitted and accepted.", "rejected_claim", 10),
            ("Underpayment Detection", "Expected vs actual reimbursement discrepancy identified and recovered.", "underpayment", 9),
            ("AI-Human Collaboration", "AI flags documentation ambiguity, human reviewer resolves, workflow resumes.", "ai_human_collab", 7),
        ]
        for name, desc, stype, total_steps in scenarios:
            sid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO demo_scenarios(id, name, description, scenario_type, total_steps, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (sid, name, desc, stype, total_steps, now(), now())
            )
            steps = _demo_scenario_steps(stype, total_steps, sid)
            for s_order, s_name, s_desc, screen in steps:
                c.execute(
                    "INSERT INTO demo_scenario_steps(id, scenario_id, step_order, step_name, step_description, status, screen, created_at) VALUES(?,?,?,?,?,'pending',?,?)",
                    (str(uuid.uuid4()), sid, s_order, s_name, s_desc, screen, now())
                )
    # Seed CLAIM-it integration config
    if c.execute("SELECT COUNT(*) n FROM claimit_integration_config").fetchone()["n"] == 0:
        # Get facility IDs for config
        fac_rows = c.execute("SELECT id, code FROM facilities WHERE is_active=1").fetchall()
        for fac in fac_rows:
            cid = str(uuid.uuid4())
            c.execute(
                """INSERT INTO claimit_integration_config(id, facility_id, integration_mode, status, last_sync, pending_transfers, pending_feedback, failed_operations, created_at, updated_at)
                VALUES(?,?,'file_based','demo',NULL,0,0,0,?,?)""",
                (cid, fac["id"], now(), now())
            )
    # Seed CLAIM-it exchange queue from existing claims
    if c.execute("SELECT COUNT(*) n FROM claimit_exchange_queue").fetchone()["n"] == 0:
        # Resolve a valid facility_id for FK constraint
        fac_row = c.execute("SELECT id FROM facilities WHERE is_active=1 LIMIT 1").fetchone()
        def_fac_id = fac_row["id"] if fac_row else None
        claims = c.execute("SELECT c.id, c.claim_number, c.patient_id, c.encounter_id, c.payer_name, c.total_charges, c.status FROM claims c").fetchall()
        statuses = [
            ("draft", "not_exported", "Prepare claim export", "Validate and package for export"),
            ("vetted", "ready_for_export", "Export prepared", "Transfer to CLAIM-it"),
            ("submitted", "awaiting_validation", "Awaiting CLAIM-it validation", "Wait for validation result"),
            ("denied", "validation_failed", "Returned for correction", "Resolve validation issues"),
            ("paid", "feedback_received", "Feedback received", "Reconcile payment"),
        ]
        for i, cl in enumerate(claims):
            cl = dict(cl)
            pnrcm_st, claimit_st, last_action, next_action = statuses[i % len(statuses)]
            ref = "CLAIM-" + str(uuid.uuid4())[:8].upper()
            c.execute(
                """INSERT INTO claimit_exchange_queue(id, claim_ref, claim_id, patient_id, encounter_id, facility_id,
                    payer_name, claim_amount, pnrcm_status, claimit_status, validation_result, last_action, next_action,
                    created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), ref, cl["id"], cl["patient_id"], cl["encounter_id"], def_fac_id,
                    cl["payer_name"] or "NHIS Demo",
                    float(cl["total_charges"] or 0),
                    pnrcm_st, claimit_st, "Pending" if claimit_st in ("not_exported","ready_for_export") else ("Passed" if claimit_st=="feedback_received" else "Failed"),
                    last_action, next_action, now(), now()
                )
            )
    # Seed CLAIM-it validation issues
    if c.execute("SELECT COUNT(*) n FROM claimit_validation_issues").fetchone()["n"] == 0:
        issues = [
            ("missing_document", "high", "Missing supporting document for emergency consultation", "claim_1", None, "Clinical note not attached to claim package", "Attach clinical documentation", "Clinical Documentation Reviewer"),
            ("charge_mismatch", "medium", "Charge/documentation mismatch on lab panel", "claim_2", None, "Lab panel charge of GHS 300 not supported by service record", "Verify service documentation", "Billing Officer"),
            ("missing_auth", "high", "Missing authorization information for radiology procedure", "claim_3", None, "Prior authorization reference not provided", "Obtain authorization from payer", "Claims Officer"),
            ("duplicate_ref", "medium", "Duplicate claim reference detected", "claim_4", None, "Claim reference CLM-AB12CD34 already submitted", "Assign new claim reference", "Claims Officer"),
            ("ready_for_transfer", "low", "Claim ready for CLAIM-it transfer", "claim_5", None, "All validation checks passed", "Proceed with export", "Claims Officer"),
        ]
        for itype, sev, desc, ref, enc_id, evidence, action, role in issues:
            c.execute(
                """INSERT INTO claimit_validation_issues(id, claim_ref, issue_type, severity, description, source_record, related_claim_id, related_encounter_id, evidence, recommended_action, responsible_role, status, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?)""",
                (str(uuid.uuid4()), ref, itype, sev, desc, "PNRCM claim validation", None, enc_id, evidence, action, role, now())
            )
    # Seed CLAIM-it file exchange
    if c.execute("SELECT COUNT(*) n FROM claimit_file_exchange").fetchone()["n"] == 0:
        files = [
            ("outbound_claims_2026_11_01.json", "outbound", "prepared", 5, None, '{"claims":5,"total":"GHS 1,850.00"}', '{"errors":0}'),
            ("inbound_feedback_2026_11_02.json", "inbound", "imported", 3, now(), '{"passed":2,"failed":1}', '{"errors":1,"reason":"validation_failed"}'),
            ("outbound_claims_2026_11_03.json", "outbound", "transferred", 4, now(), '{"claims":4,"total":"GHS 2,100.00"}', '{"errors":0}'),
        ]
        fac_rows = c.execute("SELECT id FROM facilities WHERE is_active=1 LIMIT 1").fetchall()
        fid = fac_rows[0]["id"] if fac_rows else None
        for fname, ftype, status, count, ts, valsum, errsum in files:
            c.execute(
                """INSERT INTO claimit_file_exchange(id, file_name, file_type, facility_id, claim_count, status, created_at, transferred_at, imported_at, validation_summary, error_summary, audit_trail)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), fname, ftype, fid, count, status, now(), ts if status=="transferred" else None, ts if status=="imported" else None, valsum, errsum, json.dumps([{"action":"created","at":now()}]))
            )
    # Seed CLAIM-it feedback
    if c.execute("SELECT COUNT(*) n FROM claimit_feedback").fetchone()["n"] == 0:
        feedbacks = [
            ("FB-2026-001", "CLAIM-REF-001", None, "validation", "passed", "Claim passed CLAIM-it NHIS validation", 0, None),
            ("FB-2026-002", "CLAIM-REF-002", None, "validation", "failed", "Claim returned: missing supporting document", 0, None),
            ("FB-2026-003", "CLAIM-REF-003", None, "submission", "acknowledged", "Submission acknowledged by payer", 0, None),
        ]
        for fref, cref, cid, ftype, outcome, details, processed, ptime in feedbacks:
            c.execute(
                """INSERT INTO claimit_feedback(id, feedback_ref, claim_ref, claim_id, facility_id, feedback_type, outcome, details, processed, processed_at, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), fref, cref, cid, None, ftype, outcome, details, processed, ptime, now())
            )
    # Seed CLAIM-it activity log
    if c.execute("SELECT COUNT(*) n FROM claimit_activity_log").fetchone()["n"] == 0:
        logs = [
            ("CLAIM-REF-001", None, "Claim Readiness Agent", "agent", "Readiness check completed", "not_exported", "ready_for_export", now(), "All checks passed"),
            ("CLAIM-REF-002", None, "Claims Officer", "user", "Export prepared", "ready_for_export", "export_prepared", now(), "Package created"),
            ("CLAIM-REF-002", None, "CLAIM-it Simulator", "system", "Validation failed", "export_prepared", "validation_failed", now(), "Missing document"),
        ]
        for cref, fid, actor, atype, action, prev, new, ts, ev in logs:
            c.execute(
                """INSERT INTO claimit_activity_log(id, claim_ref, facility_id, actor, actor_type, action, previous_status, new_status, timestamp, evidence)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), cref, fid, actor, atype, action, prev, new, ts, ev)
            )
    # New table indexes (safe)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_review_tasks_status ON review_tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_review_tasks_entity ON review_tasks(entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_review_tasks_assigned ON review_tasks(assigned_to)",
        "CREATE INDEX IF NOT EXISTS idx_charge_line_items_claim ON charge_line_items(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_charge_line_items_bill ON charge_line_items(bill_id)",
        "CREATE INDEX IF NOT EXISTS idx_payment_allocations_payment ON payment_allocations(payment_id)",
        "CREATE INDEX IF NOT EXISTS idx_payment_allocations_claim ON payment_allocations(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_workflow_steps_encounter ON workflow_steps(encounter_id)",
        "CREATE INDEX IF NOT EXISTS idx_authorizations_encounter ON authorizations(encounter_id)",
        "CREATE INDEX IF NOT EXISTS idx_authorizations_status ON authorizations(status)",
        "CREATE INDEX IF NOT EXISTS idx_coding_suggestions_encounter ON coding_suggestions(encounter_id)",
        "CREATE INDEX IF NOT EXISTS idx_coding_suggestions_status ON coding_suggestions(status)",
        "CREATE INDEX IF NOT EXISTS idx_claim_states_claim ON claim_states(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_facilities_code ON facilities(code)",
        "CREATE INDEX IF NOT EXISTS idx_agent_states_status ON agent_states(status)",
        "CREATE INDEX IF NOT EXISTS idx_agent_performance_agent ON agent_performance(agent_name)",
        "CREATE INDEX IF NOT EXISTS idx_agent_performance_status ON agent_performance(status)",
    ]:
        try: c.execute(idx_sql)
        except Exception: pass
    c.commit()
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_evidence_runs_claim ON evidence_analysis_runs(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_evidence_findings_claim ON evidence_findings(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_evidence_findings_run ON evidence_findings(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_evidence_citations_finding ON evidence_citations(finding_id)",
        "CREATE INDEX IF NOT EXISTS idx_chronology_claim ON claim_chronology_events(claim_id,event_time)",
        "CREATE INDEX IF NOT EXISTS idx_rcm_docs_claim ON rcm_documents(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_rcm_pages_doc ON rcm_document_pages(document_id,page_number)",
        "CREATE INDEX IF NOT EXISTS idx_evidence_feedback_finding ON evidence_feedback(finding_id)",
    ]:
        try: c.execute(idx_sql)
        except Exception: pass
    # Reconciliation indexes (safe)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_reconciliations_claim ON reconciliations(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_reconciliations_status ON reconciliations(status)",
        "CREATE INDEX IF NOT EXISTS idx_recon_line_items_recon ON reconciliation_line_items(reconciliation_id)",
        "CREATE INDEX IF NOT EXISTS idx_payment_matches_claim ON payment_matches(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_payment_matches_payment ON payment_matches(payment_id)",
        "CREATE INDEX IF NOT EXISTS idx_payment_matches_status ON payment_matches(match_status)",
        "CREATE INDEX IF NOT EXISTS idx_underpayment_cases_claim ON underpayment_cases(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_underpayment_cases_status ON underpayment_cases(status)",
        "CREATE INDEX IF NOT EXISTS idx_recovery_workflow_claim ON recovery_workflow(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_recovery_workflow_stage ON recovery_workflow(workflow_stage)",
    ]:
        try: c.execute(idx_sql)
        except Exception: pass
    # Revenue integrity indexes (safe)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_payer_rule_versions_rule ON payer_rule_versions(rule_id)",
        "CREATE INDEX IF NOT EXISTS idx_write_off_audits_claim ON write_off_audits(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_write_off_audits_status ON write_off_audits(status)",
        "CREATE INDEX IF NOT EXISTS idx_revenue_gap_cases_status ON revenue_gap_cases(status)",
        "CREATE INDEX IF NOT EXISTS idx_revenue_gap_cases_workflow ON revenue_gap_cases(responsible_workflow)",
        "CREATE INDEX IF NOT EXISTS idx_leakage_memory_pattern ON leakage_memory(pattern_type,pattern_key)",
        "CREATE INDEX IF NOT EXISTS idx_recovery_cases_claim ON recovery_cases(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_recovery_cases_status ON recovery_cases(status)",
    ]:
        try: c.execute(idx_sql)
        except Exception: pass
    # CLAIM-it integration indexes (safe)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_claimit_config_facility ON claimit_integration_config(facility_id)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_queue_claim ON claimit_exchange_queue(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_queue_ref ON claimit_exchange_queue(claim_ref)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_queue_status ON claimit_exchange_queue(claimit_status)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_queue_pnrcm ON claimit_exchange_queue(pnrcm_status)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_issues_claim ON claimit_validation_issues(related_claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_issues_status ON claimit_validation_issues(status)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_files_status ON claimit_file_exchange(status)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_feedback_claim ON claimit_feedback(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_feedback_ref ON claimit_feedback(claim_ref)",
        "CREATE INDEX IF NOT EXISTS idx_claimit_log_claim ON claimit_activity_log(claim_ref)",
    ]:
        try: c.execute(idx_sql)
        except Exception: pass
    # Revenue impact tracking indexes (safe)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_revenue_impact_impact_id ON revenue_impact_tracking(impact_id)",
        "CREATE INDEX IF NOT EXISTS idx_revenue_impact_stage ON revenue_impact_tracking(lifecycle_stage)",
        "CREATE INDEX IF NOT EXISTS idx_revenue_impact_claim ON revenue_impact_tracking(related_claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_revenue_impact_outcome ON revenue_impact_tracking(outcome_category)",
    ]:
        try: c.execute(idx_sql)
        except Exception: pass
    # Session access log indexes (safe)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_session_access_user ON session_access_log(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_session_access_session ON session_access_log(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_session_access_timestamp ON session_access_log(timestamp)",
    ]:
        try: c.execute(idx_sql)
        except Exception: pass
    # Review escalation indexes (safe)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_review_escalation_task ON review_task_escalations(review_task_id)",
        "CREATE INDEX IF NOT EXISTS idx_review_assignment_task ON review_assignments(review_task_id)",
    ]:
        try: c.execute(idx_sql)
        except Exception: pass
    c.commit()
    c.close()

def audit(c, actor, actor_type, agent_name, action, entity_type, entity_id,
          previous_state=None, new_state=None, recommendation=None, decision=None,
          execution_duration_ms=None, workflow_id=None, input_summary=None,
          output_summary=None, exception=None, human_reviewer=None, review_decision=None):
    rid = str(uuid.uuid4())
    ts = now()
    c.execute(
        """INSERT INTO audit_trail(id, timestamp, actor, actor_type, agent_name, action, entity_type, entity_id, previous_state, new_state, recommendation, decision, execution_duration_ms, created_at, workflow_id, input_summary, output_summary, exception, human_reviewer, review_decision)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rid, ts, actor, actor_type, agent_name, action, entity_type, entity_id, previous_state, new_state, recommendation, decision, execution_duration_ms, ts, workflow_id, input_summary, output_summary, exception, human_reviewer, review_decision)
    )
    return rid

def agent_execution_start(c, agent_name, task_name, entity_type, entity_id, input_data=None, workflow_id=None, orchestrator_action=None):
    eid = str(uuid.uuid4())
    ts = now()
    c.execute(
        """INSERT INTO agent_executions(id, agent_name, task_name, entity_type, entity_id, status, started_at, input_data, created_at, workflow_id, orchestrator_action)
        VALUES(?,?,?,?,?,'running',?,?,?, ?,?)""",
        (eid, agent_name, task_name, entity_type, entity_id, ts, json.dumps(input_data or {}), ts, workflow_id, orchestrator_action)
    )
    c.commit()
    return eid

def agent_execution_complete(c, eid, status, output_data=None, error_message=None,
                             human_review_required=False, duration_ms=None):
    ts = now()
    c.execute(
        """UPDATE agent_executions SET status=?, completed_at=?, output_data=?, error_message=?, human_review_required=?, duration_ms=? WHERE id=?""",
        (status, ts, json.dumps(output_data or {}), error_message, 1 if human_review_required else 0, duration_ms, eid)
    )
    c.commit()

def add_exception(c, category, severity, title, explanation, recommended_action, detecting_agent, patient_id=None, encounter_id=None, claim_id=None, bill_id=None):
    xid = str(uuid.uuid4())
    ts = now()
    c.execute(
        """INSERT INTO exceptions(id,category,severity,title,explanation,recommended_action,status,patient_id,encounter_id,claim_id,bill_id,detecting_agent,created_at,updated_at)
        VALUES(?,?,?,?,?,?,'open',?,?,?,?,?,?,?)""",
        (xid, category, severity, title, explanation, recommended_action, patient_id, encounter_id, claim_id, bill_id, detecting_agent, ts, ts)
    )
    return xid

def add_transition(c, encounter_id, from_status, to_status, actor, note=""):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO encounter_transitions(id,encounter_id,from_status,to_status,ts,actor,note) VALUES(?,?,?,?,?,?,?)",
              (tid, encounter_id, from_status, to_status, now(), actor, note))
    return tid

def add_timeline(c, patient_id, agent_name, action, detail, status="done"):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO registration_timeline(id,patient_id,ts,agent_name,action,detail,status) VALUES(?,?,?,?,?,?,?)",
              (tid, patient_id, now(), agent_name, action, detail, status))
    return tid

def dict_row(row):
    return dict(row) if row else None


def _demo_scenario_steps(scenario_type, total_steps, scenario_id):
    """Return ordered demo scenario steps as (order, name, description, screen)."""
    templates = {
        "clean_claim": [
            (1, "Patient Registration", "Register synthetic patient and validate identity", "registration"),
            (2, "Eligibility Verification", "Verify NHIS coverage and eligibility", "eligibility"),
            (3, "Clinical Documentation", "Complete clinical encounter documentation", "documentation"),
            (4, "Charge Capture", "Capture all billable services delivered", "charges"),
            (5, "AI Coding Review", "AI suggests codes; coder reviews and approves", "coding"),
            (6, "Claim Scrubbing", "Validate claim completeness and consistency", "claim_prep"),
            (7, "Claim Submission", "Submit claim to simulated payer", "claim_submit"),
            (8, "Payer Acceptance", "Payer acknowledges and accepts claim", "payer_response"),
            (9, "Payment Posting", "Record payment and match to claim", "payment"),
            (10, "Reconciliation", "Reconcile expected vs received; human approves", "reconciliation"),
        ],
        "rejected_claim": [
            (1, "Patient Registration", "Register patient with incomplete documentation", "registration"),
            (2, "Eligibility Verification", "Coverage verified but missing authorization", "eligibility"),
            (3, "Clinical Documentation", "Documentation gap flagged by AI", "documentation"),
            (4, "Charge Capture", "Services captured but claim missing supporting doc", "charges"),
            (5, "AI Coding Review", "Coding suggestion flagged for review", "coding"),
            (6, "Claim Scrubbing", "Pre-submission validation finds missing document", "claim_prep"),
            (7, "Claim Rejection", "Simulated payer rejects claim", "claim_submit"),
            (8, "AI Denial Analysis", "AI classifies rejection reason", "denial"),
            (9, "Corrective Action", "Staff adds missing documentation", "claim_prep"),
            (10, "Resubmission & Acceptance", "Claim resubmitted and accepted", "payer_response"),
        ],
        "underpayment": [
            (1, "Patient Registration", "Register patient for pharmacy encounter", "registration"),
            (2, "Eligibility Verification", "NHIS coverage verified", "eligibility"),
            (3, "Clinical Documentation", "Documentation complete", "documentation"),
            (4, "Charge Capture", "Pharmacy consultation billed", "charges"),
            (5, "AI Coding Review", "Codes reviewed and approved", "coding"),
            (6, "Claim Scrubbing", "Claim passes validation", "claim_prep"),
            (7, "Claim Submission", "Claim submitted to NHIS", "claim_submit"),
            (8, "Payer Response", "Payer approves but pays less than expected", "payer_response"),
            (9, "Underpayment Detection", "Reconciliation agent identifies discrepancy", "reconciliation"),
        ],
        "ai_human_collab": [
            (1, "Patient Registration", "Register patient", "registration"),
            (2, "Clinical Documentation", "Documentation completed with ambiguity", "documentation"),
            (3, "AI Analysis", "AI flags ambiguous documentation field", "documentation"),
            (4, "Human Review", "Reviewer clarifies the ambiguity", "review"),
            (5, "AI Approval", "AI accepts human decision", "coding"),
            (6, "Claim Preparation", "Claim prepared with corrected data", "claim_prep"),
            (7, "Submission & Payment", "Claim submitted, paid, reconciled", "payment"),
        ],
    }
    return templates.get(scenario_type, templates["clean_claim"])
