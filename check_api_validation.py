import urllib.request, json, sys

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

def check(name, status, body, expected_status=200):
    if status == expected_status:
        results["passed"] += 1
        print(f"  PASS: {name} -> {status}")
    else:
        results["failed"] += 1
        results["errors"].append(f"{name}: expected {expected_status}, got {status}, body={body}")
        print(f"  FAIL: {name} -> {status} (expected {expected_status})")

print("=" * 60)
print("PNRCM API Validation")
print("=" * 60)

# Core endpoints
print("\n--- Core ---")
s, b = get("/api/health"); check("GET /api/health", s, b)
s, b = get("/api/agents"); check("GET /api/agents", s, b)
s, b = get("/api/metrics"); check("GET /api/metrics", s, b)

# Patients
print("\n--- Patients ---")
s, b = get("/api/patients"); check("GET /api/patients", s, b)
s, b = post("/api/patients", {"mrn": "TEST-001", "name": "Test Patient", "insurance_type": "self_pay", "phone": "555-0001", "email": "test@example.com", "address": "123 Test St"}); check("POST /api/patients", s, b, 201)

# Encounters
print("\n--- Encounters ---")
s, b = get("/api/encounters"); check("GET /api/encounters", s, b)
s, b = post("/api/encounters", {"patient_id": "TEST-001", "encounter_type": "outpatient", "chief_complaint": "Test"}); check("POST /api/encounters", s, b, 201)

# Bills
print("\n--- Bills ---")
s, b = get("/api/bills"); check("GET /api/bills", s, b)
s, b = post("/api/bills", {"patient_id": "TEST-001", "amount": 100.0}); check("POST /api/bills", s, b, 201)

# Claims
print("\n--- Claims ---")
s, b = get("/api/claims"); check("GET /api/claims", s, b)
s, b = post("/api/claims", {"patient_id": "TEST-001", "total_amount": 100.0}); check("POST /api/claims", s, b, 201)

# Payments (missing GET /api/payments)
print("\n--- Payments ---")
s, b = get("/api/payments"); check("GET /api/payments (MISSING ENDPOINT)", s, b, 404)
s, b = post("/api/payments/match", {"claim_id": "test-claim", "payment_id": "test-payment", "matched_amount": 50.0}); check("POST /api/payments/match", s, b)
s, b = get("/api/collections/payments"); check("GET /api/collections/payments", s, b)

# Denials
print("\n--- Denials ---")
s, b = get("/api/denials"); check("GET /api/denials", s, b)
s, b = post("/api/denials/detect", {}); check("POST /api/denials/detect", s, b)

# Appeals
print("\n--- Appeals ---")
s, b = get("/api/appeals"); check("GET /api/appeals", s, b)
s, b = post("/api/appeals/detect", {}); check("POST /api/appeals/detect", s, b)

# Follow-ups
print("\n--- Follow-ups ---")
s, b = get("/api/followups"); check("GET /api/followups", s, b)
s, b = post("/api/followups/ensure", {}); check("POST /api/followups/ensure", s, b)

# Collections
print("\n--- Collections ---")
s, b = get("/api/collections/balances"); check("GET /api/collections/balances", s, b)
s, b = post("/api/collections/balances/ensure", {}); check("POST /api/collections/balances/ensure", s, b)
s, b = post("/api/collections/payments", {"balance_id": "test", "amount": 10.0}); check("POST /api/collections/payments", s, b)

# Leakage
print("\n--- Leakage ---")
s, b = get("/api/leakage"); check("GET /api/leakage", s, b)

# Reconciliation
print("\n--- Reconciliation ---")
s, b = get("/api/underpayments"); check("GET /api/underpayments", s, b)
s, b = post("/api/underpayments/detect", {}); check("POST /api/underpayments/detect", s, b)

# Analytics
print("\n--- Analytics ---")
s, b = get("/api/analytics/overview"); check("GET /api/analytics/overview", s, b)
s, b = get("/api/analytics/claims"); check("GET /api/analytics/claims", s, b)
s, b = get("/api/analytics/revenue"); check("GET /api/analytics/revenue", s, b)
s, b = get("/api/analytics/leakage"); check("GET /api/analytics/leakage", s, b)
s, b = get("/api/analytics/ar-aging"); check("GET /api/analytics/ar-aging", s, b)
s, b = get("/api/analytics/agents"); check("GET /api/analytics/agents", s, b)

# Revenue Integrity
print("\n--- Revenue Integrity ---")
s, b = get("/api/integrity/alerts"); check("GET /api/integrity/alerts", s, b)
s, b = post("/api/integrity/scan", {}); check("POST /api/integrity/scan", s, b)
s, b = get("/api/integrity/metrics"); check("GET /api/integrity/metrics", s, b)

# Orchestrator
print("\n--- Orchestrator ---")
s, b = get("/api/orchestrator/state"); check("GET /api/orchestrator/state", s, b)
s, b = get("/api/orchestrator/activity"); check("GET /api/orchestrator/activity", s, b)
s, b = get("/api/orchestrator/tasks"); check("GET /api/orchestrator/tasks", s, b)

# Workflows
print("\n--- Workflows ---")
s, b = post("/api/workflows/run", {"scenario": "oop", "patient": "API Test Patient"}); check("POST /api/workflows/run", s, b)
s, b = post("/api/reset", {}); check("POST /api/reset", s, b)

print()
print("=" * 60)
print(f"RESULTS: {results['passed']} passed, {results['failed']} failed")
if results["errors"]:
    print("\nErrors:")
    for e in results["errors"]:
        print(f"  - {e}")
print("=" * 60)

sys.exit(1 if results["failed"] > 0 else 0)