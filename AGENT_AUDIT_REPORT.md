# PNRCM Revenue OS — AI Agent Audit Report

## Scope
Audited all 14 agent modules in `agents/`, their API routes in `app.py`, database models in `db.py`, and UI components in `index.html`.

---

## 1. Agent Inventory

| # | Agent Module | Implements Real Work? | DB Tables Used | API Endpoints | UI Component |
|---|---|---|---|---|---|
| 1 | `orchestrator.py` | Partial — `register_patient_full()` executes logic; `AGENT_STATES` is in-memory only (lost on restart) | `patients`, `registration_timeline`, `agent_executions`, `exceptions`, `audit_trail` | `/api/orchestrator/state`, `/api/orchestrator/activity` | AI Orchestrator section (static cards only) |
| 2 | `documentation.py` | Yes — reviews encounters, persists `documentation_reviews`, requires human resolution | `documentation_reviews`, `encounters`, `audit_trail` | `/api/encounters/<eid>/documentation-review`, `/api/documentation-reviews/*` | Documentation & Charges section |
| 3 | `billing.py` | Yes — generates bills, validates totals, detects duplicates/missing charges | `bills`, `services`, `audit_trail` | `/api/bills`, `/api/bills/<bid>/validate`, `/api/encounters/<eid>/bill` | Documentation & Charges section |
| 4 | `claims_vetting.py` | Yes — vets claims against 7 check categories, persists `claim_vetting` | `claim_vetting`, `claims`, `audit_trail` | `/api/claims/<cid>/vet`, `/api/claims/<cid>/standards` | Claim Preparation section |
| 5 | `denial.py` | Yes — detects denied claims, persists `denials`, categorizes, corrective actions | `denials`, `claims`, `audit_trail` | `/api/denials/detect`, `/api/denials`, `/api/denials/<did>/resolve` | Denial Management section |
| 6 | `leakage.py` | **No persistence** — read-time detection only, returns snapshot, writes nothing | None (reads `services`, `claims`, `bills`, `payments`) | `/api/leakage` | Revenue Integrity section |
| 7 | `reconciliation.py` | Yes — full reconciliation engine, payment matching, underpayment detection | `reconciliations`, `payment_matches`, `underpayment_cases`, `recovery_workflow`, `audit_trail` | `/api/reconciliations/*`, `/api/payments/match`, `/api/underpayments/*`, `/api/payment/simulated`, `/api/payer-response/simulated` | Payments & Reconciliation section |
| 8 | `revenue_integrity.py` | Yes — service-to-revenue, closed claim leakage, payer drift, prioritization, gaps, write-off audits, recovery cases | `revenue_gap_cases`, `leakage_cases`, `write_off_audits`, `recovery_cases`, `payer_rule_versions`, `leakage_memory`, `audit_trail` | `/api/revenue-integrity/*` | Revenue Integrity section |
| 9 | `followup.py` | Yes — creates follow-ups, tracks attempts, detects overdue | `claim_followups`, `claim_followup_attempts`, `exceptions` | `/api/followups/*` | AR & Follow-Up section |
| 10 | `collections.py` | Yes — patient balances, payments, reversals, collection tasks, communication templates | `patient_balances`, `patient_payments`, `collection_tasks`, `collection_attempts`, `copayment_rules` | `/api/collections/*` | Patient Finance section |
| 11 | `appeals.py` | Yes — draft appeal letters, tracks appeal lifecycle, human approval required | `appeals`, `exceptions`, `audit_trail` | `/api/appeals/detect`, `/api/appeals`, `/api/appeals/<aid>/status` | Denial Management section |
| 12 | `analytics.py` | Yes — derived metrics only (read-only), no write operations | None (reads all tables) | `/api/analytics/*`, `/api/reports/*.csv` | Analytics & Reports section |
| 13 | `evidence.py` | Yes — document storage, chronology, findings, citations, review decisions | `rcm_documents`, `rcm_document_pages`, `evidence_analysis_runs`, `evidence_findings`, `evidence_citations`, `evidence_review_decisions`, `evidence_feedback`, `claim_chronology_events` | `/api/evidence/*` | Evidence Intelligence (embedded in Claims) |
| 14 | `fraud.py` | Yes — anomaly detection, alerts, investigations, integrity metrics | `fraud_alerts`, `fraud_investigations`, `audit_trail` | `/api/integrity/scan`, `/api/integrity/alerts/*`, `/api/integrity/metrics` | Revenue Integrity section |

---

## 2. Agents That Are Passive / UI Cards Only

**AI Orchestrator (`orchestrator.py`):**
- `AGENT_STATES` is a Python dict in memory — lost on server restart
- No persistence of agent status, current task, or execution history beyond `agent_executions` table (seeded with static rows)
- No task creation, assignment, delegation, or routing logic
- Agent cards show static `tasks_completed`/`exceptions` counters from in-memory state only
- **Gap:** No agent detail view; clicking an agent does nothing

**Revenue Leakage Detection Agent (`leakage.py`):**
- `detect_leakage()` performs read-time detection only; persists nothing
- No `leakage_cases` records created by this agent (that table is written by `revenue_integrity.py`)
- No task creation, no human routing, no status tracking
- **Gap:** Detected leakage has no actionable output — findings vanish after the API call

---

## 3. Missing Agents (from the required list)

| Required Agent | Status |
|---|---|
| Patient Access & Registration Agent | Partially — `orchestrator.register_patient_full()` handles registration but has no dedicated module or persistent task lifecycle |
| Eligibility & Coverage Agent | **Missing** — no module exists; insurance_type is stored on patients but no eligibility checking logic |
| Prior Authorization Agent | **Missing** — `authorizations` table exists with CRUD routes, but no agent logic for identifying auth-required services, tracking expiry, or flagging gaps |
| Encounter & Admission Coordination Agent | Partially — encounter CRUD + status transitions exist, but no inpatient/admission episode tracking, no ward/bed support |
| Clinical Documentation Agent | Yes — `documentation.py` |
| Medical Coding Review Agent | Partially — `documentation.py` handles docs; `clinical_suggestions` table exists but no dedicated coding review agent |
| Charge Capture Agent | Partially — `billing.py` detects missing charges but no dedicated charge capture module |
| Claims Preparation & Scrubbing Agent | Yes — `claims_vetting.py` |
| Claims Submission & Payer Response Agent | Partially — `reconciliation.record_simulated_payer_response()` exists but no submission tracking agent |
| Denial Management Agent | Yes — `denial.py` |
| Accounts Receivable & Follow-Up Agent | Yes — `followup.py` + `collections.py` |
| Payment & Reconciliation Agent | Yes — `reconciliation.py` |
| Revenue Integrity Monitoring Agent | Yes — `revenue_integrity.py` + `fraud.py` (renamed) |
| Inpatient Revenue Management Agent | **Missing** — no admission episodes, wards, beds, long-stay tracking, interim billing |
| Analytics & Revenue Intelligence Agent | Yes — `analytics.py` + `revenue_integrity.revenue_integrity_analytics()` |
| Human Review & Escalation Agent | Partially — `review_tasks` table + resolution routes exist, but no dedicated human review agent with workload-aware routing |

---

## 4. Overlapping Responsibilities

| Overlap | Agents Involved | Resolution |
|---|---|---|
| Duplicate charge detection | `billing.py` (`_service_duplicates`), `leakage.py`, `revenue_integrity.py` (`check_service_to_revenue_completeness`) | Consolidate into `revenue_integrity.py` as the single source; `billing.py` and `leakage.py` remain as read-time detectors |
| Missing charges detection | `billing.py` (`_missing_charges`), `claims_vetting.py` (`_missing_charges`), `leakage.py`, `evidence.py` | Same — `revenue_integrity.py` is authoritative; others are detectors |
| Underpayment detection | `reconciliation.py` (`detect_underpayments`), `revenue_integrity.py` (`detect_closed_claim_leakage`), `leakage.py` | Same — `reconciliation.py` is authoritative for underpayment cases |
| Denial categorization | `denial.py` (`_categorize`), `appeals.py` (`_categorize`) | Different category taxonomies; keep both but align labels |
| Revenue gap detection | `revenue_integrity.py` (multiple functions) | Consolidate under `revenue_integrity.py` |
| Fraud/integrity alerts | `fraud.py`, `revenue_integrity.py` (`add_exception`) | `fraud.py` renamed to Revenue Integrity Agent; alerts routed to exceptions table |

---

## 5. Agents Disconnected from Revenue-Cycle Workflow

- **Eligibility & Coverage Agent**: Not implemented — no trigger from registration or encounter creation
- **Prior Authorization Agent**: Not implemented — `authorizations` table exists but no agent logic connects it to claims submission
- **Inpatient Revenue Management Agent**: Not implemented — no admission/ward/bed/long-stay tracking
- **Revenue Leakage Detection Agent**: Read-only, no persistence, no task creation — findings are not actionable

---

## 6. Missing Task Execution / Persistence / Error Handling / Escalation

| Gap | Details |
|---|---|
| No agent detail view | Agents are static cards; no way to inspect current task, history, findings, outputs |
| No task lifecycle states | Tasks lack Pending/Assigned/In Progress/Awaiting Human Review/Blocked/Completed/Failed/Cancelled states |
| No agent-to-agent delegation | No mechanism for one agent to delegate work to another |
| No human-to-agent routing | No way for a human to send a task to a specific agent |
| No workload-aware routing | No agent capacity or workload tracking |
| No retry/failure handling | `agent_executions` has `error_message` but no retry logic |
| No idempotency on task creation | Some agents check for existing records (e.g., `ensure_followup`, `detect_denials`), but no universal deduplication |
| Agents not event-driven | No triggers on encounter completion, claim draft, payment record, etc. |
| No agent performance metrics per agent | `analytics.agent_performance()` exists but is not surfaced in agent detail views |

---

## 7. Financial Consistency Issues

| Issue | Location |
|---|---|
| `analytics.overview()` uses `max(collected_claims, collected_bills)` to avoid double counting — inconsistent definition | `app.py` dashboard overview |
| `patient_payments` table separate from `payments` table — two payment models | `db.py` |
| `bills.outstanding` not always updated when `patient_payments` recorded | `collections.py` updates balances but `bills` table not updated |
| Some seeded claims have `paid_amount=0` but `status='paid'` | Seed data inconsistency |
| `GHS NaN` possible when `total_charges` is null | Frontend `fmtMoney()` handles null but some aggregations may not |

---

## 8. Empty Tables After Seeding

| Table | Rows After Seed | Issue |
|---|---|---|
| `payments` | 0 | Separate from `patient_payments`; no seed logic |
| `leakage_cases` | 0 | Written by `revenue_integrity.py` but `leakage.py` doesn't write to it |
| `clinical_suggestions` | 25 | Seeded but no coding review agent UI |

---

## 9. Workflow Trace (Registration → Payment)

1. **Registration**: `POST /api/patients` → `orch.register_patient_full()` creates patient, runs identity check, creates `agent_executions`, `registration_timeline`
2. **Encounter**: `POST /api/encounters` → creates encounter, auto-creates copay balance via `collections.ensure_balance()`, adds transition
3. **Services**: `POST /api/encounters/<eid>/services` → adds billable services
4. **Documentation Review**: `POST /api/encounters/<eid>/documentation-review` → `documentation.review_documentation()` creates `documentation_reviews`
5. **Billing**: `POST /api/encounters/<eid>/bill` → `billing.generate_bill()` creates draft bill
6. **Claim**: `POST /api/claims` → creates claim; `POST /api/claims/<cid>/vet` → `claims_vetting.vet_claim()`
7. **Submission**: `POST /api/claims/<cid>/submit` → changes status to submitted
8. **Payer Response**: `POST /api/payer-response/simulated` → updates claim status
9. **Payment**: `POST /api/payment/simulated` → creates `payments` record
10. **Reconciliation**: `POST /api/reconciliations/run` → `reconciliation.reconcile_claim()` creates reconciliation + payment matches
11. **Denial**: `POST /api/denials/detect` → `denial.detect_denials()` creates denial records
12. **Appeal**: `POST /api/appeals/detect` → `appeals.detect_and_create_appeals()` creates appeal drafts
13. **Follow-up**: `POST /api/followups/ensure` → `followup.ensure_followup()` creates follow-up tasks
14. **Collections**: `POST /api/collections/balances/ensure` → creates patient balances

**Missing triggers:** Steps 2-6 do not auto-trigger downstream agents (e.g., completed encounter → documentation review; claim draft → claims vetting; payment → reconciliation).

---

## Summary of Required Changes

1. **AI Orchestrator**: Make it functional with persistent task lifecycle, agent-to-agent delegation, agent-to-human routing, human-to-agent requests, workload-aware routing, retry/failure handling, execution history
2. **Agent Detail Views**: Click an agent → modal with status, current task, active/pending/completed/failed tasks, execution history, findings, outputs, errors
3. **Missing Agents**: Eligibility & Coverage, Prior Authorization, Inpatient Revenue Management, Coding Review, Charge Capture
4. **Event-Driven Triggers**: Encounter completion → documentation review; claim draft → vetting; payment → reconciliation; denial → appeal/follow-up
5. **Demo Data**: Populate empty tables, add inpatient/long-stay cases, idempotent seed/reset
6. **Financial Consistency**: Fix NaN/0.00 values, unify payment model, consistent aggregations
7. **Human Oversight**: Clear distinction between AI recommendation, AI-prepared, human-approved, executed actions
