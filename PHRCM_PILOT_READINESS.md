# PHRCM Pilot Readiness

**Status classification:** Prototype **verified** · Controlled **pilot-ready** · **Not** production-ready.

This document describes the verified state of PHRCM for a controlled pilot with a private hospital in Ghana. Every claim below reflects implemented and tested behavior; no control is claimed that is not implemented.

---

## 1. Current System Capabilities
- Revenue cycle: registration/eligibility, encounters, documentation & charge capture, billing (with line items), claims preparation/submission, denials/appeals, payments, reconciliation, AR/follow-up.
- Revenue integrity: leakage detection & resolution, revenue impact tracking, revenue command center.
- Financial Analytics & Trends Agent (read-only, data-derived): revenue/payer/claims/AR/leakage trends, forecast (estimate), anomalies, management insights; facility-scoped; CSV export.
- AI Orchestrator: 12 registered agents (rule-based/simulated; **not** LLM-driven), task lifecycle, execution history, human review routing.

## 2. Security Controls Verified
- **RBAC (server-side):** centralized `before_request` enforcement; domain role sets; verified coding/billing/finance/claims/admin restrictions (403). Not UI-only.
- **Facility isolation:** `facility_id` on patients/encounters/bills/claims/payments/denials/reconciliations/followups; list + analytics + audit scoped; cross-facility payment match rejected. Verified with 2 facilities.
- **Rate limiting:** per-IP sliding window (default 240/min, analytics 120/min, export 30/min, auth 20/min); returns 429 + Retry-After. Verified.
- **Security headers:** `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Content-Security-Policy`. Verified.
- **Audit trail:** actor/entity/action/prev/new/timestamp/reason/facility; facility-scoped history; financial mutations logged.
- **Error handling:** global 404/405/500 JSON handlers; no stack traces to clients.
- **Auth modes:** demo (default, anonymous allowed) vs secure (`PNRCM_SECURE_MODE=1` rejects anonymous/unknown users on protected mutations).
- **CSV export:** facility + date scoped; formula-injection guarded.

## 3. Authentication & Session (current design)
- Identity is passed via `X-User` header (or `actor` in body). **No cookie-based sessions** are used, therefore:
  - **CSRF is not applicable** to the current header-based pattern (no ambient cookie authority). Documented rationale; if cookie auth is added later, CSRF tokens must be introduced.
  - Passwords/session tokens: PHRCM does not store passwords in this prototype; users are pre-provisioned in `user_roles`. **No default production credentials** are included.
  - `PNRCM_SECRET_KEY` must be set when `PNRCM_SECURE_MODE=1` (validated at startup).

## 4. Deployment Requirements
- Python 3, Flask, SQLite (WAL). Single-process (SQLite single-writer).
- `pip install -r requirements.txt`.
- `PNRCM_DB` (path), `PORT`, `PNRCM_SECURE_MODE`, `PNRCM_SECRET_KEY`, `PNRCM_RATE_LIMIT`.
- See `.env.example`. **Do not commit secrets.**

## 5. Facility Setup Procedure
1. Insert facility rows (`facilities`).
2. Create users in `user_roles` with `role` + `facility_id` + `is_active=1`.
3. Assign roles per least privilege (see role map).
4. Seed synthetic demo data only for evaluation (`/api/demo/seed-all?force=1`).

## 6. User Creation & Role Assignment
Roles: `administrator`, `facility_administrator`, `revenue_cycle_manager`, `finance_officer`, `billing_officer`, `claims_officer`, `medical_coder`, `clinical_documentation_reviewer`, `registration_officer`. Assign the minimum role required; finance/billing/claims separated.

## 7. Backup & Recovery
- SQLite file `pnrcm_demo.db` (+ `-wal`, `-shm`). Back up all three while the app is stopped, or use `sqlite3 .backup`.
- Recovery: restore the `.db` (and WAL) files; run `db.init_db()` to re-apply additive migrations.
- Recommended: scheduled file copy + periodic `sqlite3` integrity check.

## 8. Known Limitations
- SQLite single-writer; concurrent writes can lock (WAL mitigates). Not for high-concurrency production.
- Auth is header-based (no password/session hardening, no CSRF tokens) — appropriate for a controlled pilot, **not** production.
- Agents are rule-based/simulated, not LLM-driven.
- Forecast is a simple linear model (no seasonality); department-level analytics unsupported by schema.
- No rate limiting across multiple processes (in-memory, single-process).
- No external monitoring/alerting/backup automation.

## 9. Pilot Setup Checklist
- [ ] Set `PNRCM_SECURE_MODE=1` and a strong `PNRCM_SECRET_KEY` for the pilot.
- [ ] Create pilot facility + users with least-privilege roles.
- [ ] Load synthetic demo data; verify no real patient data.
- [ ] Confirm security headers (curl -I).
- [ ] Confirm rate limiting (429 on burst).
- [ ] Confirm RBAC (role-restricted actions 403).
- [ ] Confirm facility isolation (2-facility test).
- [ ] Configure backups (file copy of db + WAL).
- [ ] Verify `/api/health` and core workflows via UI.
- [ ] Establish monitoring (logs) and incident contact.

## 10. Data Handling Precautions
- Synthetic data only for the pilot unless a signed data-processing agreement exists.
- Minimize patient-identifiable data in audit summaries.
- Do not expose `PNRCM_SECRET_KEY` or DB path in error messages.

## 11. Required Monitoring
- Application logs (errors, 429s, 403s, 5xx).
- Disk space for the SQLite file + WAL.
- Backup success.
- Failed-login / unauthorized-access frequency.

## 12. Remaining Blockers (for production, not pilot)
- Real authentication/session management + CSRF (if cookies adopted).
- PostgreSQL or equivalent for multi-writer concurrency.
- Secret management, TLS, reverse proxy, rate limiting at the edge.
- Monitoring/alerting, automated backup/restore drills, audit immutability.
- Accessibility/responsive QA and browser-based E2E.

---
**Verdict:** **Controlled pilot ready** (with secure mode + backups). **Not production ready.**