import urllib.request, json, sys, time

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

# Reset first
print("\n--- Reset ---")
s, b = post("/api/reset", {})
check("Reset API", s == 200, str(b))

# 1. Patient Registration (orchestrated)
print("\n--- Patient Registration ---")
s, b = post("/api/patients", {"mrn": "VAL-001", "name": "Validation Patient", "insurance_type": "self_pay", "phone": "555-0001", "email": "val@example.com", "address": "123 Val St"})
check("Create patient", s in (200, 201), str(b))
mrn = b.get("mrn") or b.get("id") or "VAL-001"

# 2. Encounter Creation
print("\n--- Encounter Creation ---")
s, b = post("/api/encounters", {"patient_id": mrn, "encounter_type": "outpatient", "chief_complaint": "Validation test"})
check("Create encounter", s in (200, 201), str(b))
enc_id = b.get("encounter_id") or b.get("id")

# 3. Services
print("\n--- Services ---")
s, b = post(f"/api/encounters/{enc_id}/services", {"code": "99213", "description": "Office visit", "amount": 150.0, "quantity": 1})
check("Add service", s in (200, 201), str(b))

# 4. Documentation Review (triggered)
print("\n--- Documentation Review ---")
s, b = post(f"/api/encounters/{enc_id}/documentation-review", {})
check("Documentation review", s in (200, 201), str(b))

# 5. Billing
print("\n--- Billing ---")
s, b = post(f"/api/encounters/{enc_id}/bill", {})
check("Generate bill", s in (200, 201), str(b))

# 6. Claim Creation + Vetting (triggered)
print("\n--- Claim Creation + Vetting ---")
s, b = post("/api/claims", {"patient_id": mrn, "encounter_id": enc_id, "total_amount": 150.0})
check("Create claim", s in (200, 201), str(b))
claim_id = b.get("claim_id") or b.get("id")

s, b = post(f"/api/claims/{claim_id}/vet", {})
check("Vet claim", s in (200, 201), str(b))

# 7. Prior Authorization
print("\n--- Prior Authorization ---")
s, b = post(f"/api/prior-auth/encounters/{enc_id}/task", {})
check("Prior auth task", s in (200, 201), str(b))

# 8. Payment + Reconciliation (triggered)
print("\n--- Payment + Reconciliation ---")
s, b = post("/api/payment/simulated", {"claim_id": claim_id, "amount": 150.0, "payment_method": "Cash", "reference": "VAL-PAY-001"})
check("Record simulated payment", s in (200, 201), str(b))

s, b = post("/api/underpayments/detect", {})
check("Detect underpayments", s in (200, 201), str(b))

# 9. Leakage Detection (with tasks)
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

# 15. Orchestrator metrics
print("\n--- Orchestrator ---")
s, b = get("/api/orchestrator/metrics")
check("Orchestrator metrics", s in (200, 201), str(b))

# 16. Workflow run (full scenario)
print("\n--- Workflow Run ---")
s, b = post("/api/workflows/run", {"scenario": "oop", "patient": "Workflow Patient"})
check("Workflow run oop", s == 200, str(b))

# 17. Financial consistency: check for NaN/null in analytics
print("\n--- Financial Consistency ---")
s, b = get("/api/analytics/overview")
if s == 200:
    def has_nan(obj):
        if isinstance(obj, dict):
            return any(has_nan(v) for v in obj.values())
        if isinstance(obj, list):
            return any(has_nan(v) for v in obj)
        if isinstance(obj, float):
            import math
            return math.isnan(obj) or math.isinf(obj)
        return False
    check("No NaN/Inf in analytics overview", not has_nan(b), str(b)[:200])

print()
print("=" * 60)
print(f"RESULTS: {results['passed']} passed, {results['failed']} failed")
if results["errors"]:
    print("\nErrors:")
    for e in results["errors"]:
        print(f"  - {e}")
print("=" * 60)

sys.exit(1 if results["failed"] > 0 else 0)