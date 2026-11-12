"""PNRCM End-to-End Validation.

Exercises the complete revenue-cycle workflow through the live API
(http://localhost:5000) using ONE consistent patient and encounter. Does NOT
reset the database between dependent workflow steps.

Run the server first (e.g. `python app.py`), then run this script:

    python final_validation.py
"""
import urllib.request, json, sys, math

BASE = "http://localhost:5000"
results = {"passed": 0, "failed": 0, "errors": []}


def get(path):
    try:
        req = urllib.request.Request(BASE + path, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return None, {"error": str(e)}


def post(path, data=None):
    try:
        body = json.dumps(data if data is not None else {}).encode()
        req = urllib.request.Request(BASE + path, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return None, {"error": str(e)}


def check(name, cond, detail=""):
    if cond:
        results["passed"] += 1
        print(f"  PASS: {name}" + (f" — {detail}" if detail else ""))
    else:
        results["failed"] += 1
        results["errors"].append(f"{name}: {detail}")
        print(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))


print("=" * 60)
print("PNRCM Final Validation (End-to-End)")
print("=" * 60)

# NOTE: We do NOT reset here. We use one consistent patient/encounter through
# the whole workflow so dependent IDs are extracted from real responses.
# Use a unique MRN per run to avoid "Patient ID already exists" collisions.
import uuid as _uuid
run_mrn = "E2E-" + str(_uuid.uuid4())[:8].upper()

# 1. Patient Registration
print("\n--- Patient Registration ---")
s, b = post("/api/patients", {
    "first_name": "Validation", "last_name": "Patient",
    "patient_id": run_mrn, "phone": "555-0001",
    "email": "e2e@example.com", "address": "123 E2E St",
    "insurance_type": "self_pay",
})
check("Create patient", s in (200, 201), str(b))
# Orchestrated registration nests the patient row under b["patient"].
patient = b.get("patient", b) if isinstance(b, dict) and not b.get("error") else b
if b.get("error"):
    # MRN already existed from a prior run; look it up.
    s, b = get(f"/api/patients/{run_mrn}")
    check("Look up existing patient", s == 200, str(b))
    patient = b.get("patient", b) if isinstance(b, dict) else b
mrn = patient.get("patient_id") or run_mrn
patient_row_id = patient.get("id")
check("patient has id", patient_row_id is not None, str(b)[:120])

# 2. Encounter Creation
print("\n--- Encounter Creation ---")
s, b = post("/api/encounters", {
    "patient_id": mrn,
    "encounter_type": "outpatient",
    "reason": "E2E validation encounter",
})
check("Create encounter", s in (200, 201), str(b))
enc_id = b.get("encounter_id") or b.get("id")
check("Encounter has id", enc_id is not None)

# 3. Services (charge capture)
print("\n--- Services ---")
s, b = post(f"/api/encounters/{enc_id}/services", {
    "service_name": "Office Visit",
    "description": "E2E office visit",
    "quantity": 1,
    "unit_price": 150.0,
})
check("Add service", s in (200, 201), str(b))
service_id = b.get("id")
check("Service has id", service_id is not None)
check("Service total_price", b.get("total_price") == 150.0, str(b.get("total_price")))

# 4. Documentation Review (triggered by encounter completion)
print("\n--- Documentation Review ---")
s, b = post(f"/api/encounters/{enc_id}/documentation-review", {})
check("Documentation review", s in (200, 201), str(b))

# 5. Encounter complete (triggers documentation review task)
print("\n--- Encounter Complete ---")
s, b = post(f"/api/encounters/{enc_id}/complete", {})
check("Complete encounter", s in (200, 201), str(b))

# 6. Billing
print("\n--- Billing ---")
s, b = post("/api/bills", {
    "encounter_id": enc_id,
    "patient_id": patient_row_id,
    "subtotal": 150.0,
    "tax": 0.0,
    "discount": 0.0,
})
check("Generate bill", s in (200, 201), str(b))
bill_id = b.get("bill_number") or b.get("id")
check("Bill has number/id", bill_id is not None)
check("Bill outstanding equals total", b.get("outstanding") == 150.0, str(b.get("outstanding")))

# 7. Claim Creation + Vetting
print("\n--- Claim Creation + Vetting ---")
s, b = post("/api/claims", {
    "encounter_id": enc_id,
    "patient_id": patient_row_id,
    "total_charges": 150.0,
    "payer_type": "self_pay",
})
check("Create claim", s in (200, 201), str(b))
claim_id = b.get("claim_number") or b.get("id")
check("Claim has number/id", claim_id is not None)

s, b = post(f"/api/claims/{claim_id}/vet", {})
check("Vet claim", s in (200, 201), str(b))

# 8. Payment + Reconciliation
print("\n--- Payment + Reconciliation ---")
s, b = post(f"/api/bills/{bill_id}/pay", {
    "amount": 150.0,
    "payment_method": "Cash",
    "reference": "E2E-PAY-001",
})
check("Record simulated payment", s in (200, 201), str(b))
if isinstance(b, dict):
    check("Payment paid = 150", b.get("paid") == 150.0, str(b.get("paid")))
    check("Payment outstanding = 0", b.get("outstanding") == 0.0, str(b.get("outstanding")))
    check("Payment status = paid", b.get("status") == "paid", str(b.get("status")))

s, b = post("/api/underpayments/detect", {})
check("Detect underpayments", s in (200, 201), str(b))

# 9. Leakage Detection
print("\n--- Leakage Detection ---")
s, b = get("/api/leakage")
check("Leakage detection", s in (200, 201), str(b))

# 10. Denials
print("\n--- Denials ---")
s, b = post("/api/denials/detect", {})
check("Detect denials", s in (200, 201), str(b))

# 11. Appeals
print("\n--- Appeals ---")
s, b = post("/api/appeals/detect", {})
check("Detect appeals", s in (200, 201), str(b))

# 12. Follow-up
print("\n--- Follow-up ---")
s, b = post("/api/followups/ensure", {})
check("Ensure follow-ups", s in (200, 201), str(b))

# 13. Collections
print("\n--- Collections ---")
s, b = post("/api/collections/balances/ensure", {})
check("Ensure balances", s in (200, 201), str(b))

# 14. Analytics
print("\n--- Analytics ---")
s, b = get("/api/analytics/overview")
check("Analytics overview", s == 200, str(b))
if s == 200:
    def has_nan(obj):
        if isinstance(obj, dict):
            return any(has_nan(v) for v in obj.values())
        if isinstance(obj, list):
            return any(has_nan(v) for v in obj)
        if isinstance(obj, float):
            return math.isnan(obj) or math.isinf(obj)
        return False
    check("No NaN/Inf in analytics overview", not has_nan(b), str(b)[:200])

# 15. Orchestrator metrics (state/activity/tasks)
print("\n--- Orchestrator ---")
s, b = get("/api/orchestrator/state")
check("Orchestrator state", s in (200, 201), str(b))
s, b = get("/api/orchestrator/activity")
check("Orchestrator activity", s in (200, 201), str(b))
s, b = get("/api/orchestrator/tasks")
check("Orchestrator tasks", s in (200, 201), str(b))

# 16. Workflow run (full scenario) - separate patient so it doesn't disturb E2E
print("\n--- Workflow Run ---")
s, b = post("/api/workflows/run", {"scenario": "oop", "patient": "Workflow Patient"})
check("Workflow run oop", s == 200, str(b))

print()
print("=" * 60)
print(f"RESULTS: {results['passed']} passed, {results['failed']} failed")
if results["errors"]:
    print("\nErrors:")
    for e in results["errors"]:
        print(f"  - {e}")
print("=" * 60)

sys.exit(1 if results["failed"] > 0 else 0)
