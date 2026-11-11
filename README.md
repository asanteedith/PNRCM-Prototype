# PNRCM functional workflow prototype

This version is an actual web application backed by a SQLite database. Workflow runs, agent event traces, and human review decisions are persisted.

## Run locally

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000.

## Functional scope
- Run self-pay / simulated Mobile Money, NHIS validation, and missed-charge scenarios
- Backend workflow execution with agent-specific structured output and branching
- Persist runs, events, and review queue to SQLite
- Approve or reject a review item; decision appears in the run's event log
- Run history and summary metrics from API
- Reset demo data

All data is synthetic. Payment, claims, clinical systems, NHIS and insurer integrations are not connected. Agent execution is deterministic workflow logic; no LLM API key is required. Not production ready.
