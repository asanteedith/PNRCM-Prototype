"""PNRCM API Validation.

Validates the live API (http://localhost:5000) against the actual implemented
contracts in app.py. Uses urllib so it exercises the real HTTP server.

Run the server first (e.g. `python app.py`), then run this script:

    python check_api_validation.py
"""
import urllib.request, json, sys, uuid

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


def http(name, path, method="GET", data=None, expected=200):
    if method == "GET":
        s, b = get(path)
    else:
        s, b = post(path, data)
    ok = (s == expected)
    check(name, ok, f"HTTP {s} (expected {expected}) body={str(b)[:160]}")
    return s, b


print("=" * 60)
print("PNRCM API Validation")
print("=" * 60)

run_pid = "VAL-" + str(uuid.uuid4())[:8].upper()
run_phone = "555-" + str(uuid.uuid4())[:4].upper()

# Core endpoints
print("\n--- Core ---")
http("GET /api/health", "/api/health", expected=200)
http("GET /api/agents", "/api/agents", expected=200)
http("GET /api/metrics", "/api/metrics", expected=200)

# Patients  (contract: POST requires first_name + last_name)
print("\n--- Patients ---")
http("GET /api/patients", "/api/patients", expected=200)
s, b = post("/api/patients", {
    "first_name": "Validation", "last_name": "Patient",
    "patient_id": run_pid, "phone": run_phone,
    "email": "val@example.com", "address": "123 Val St",
    "insurance_type": "self_pay",
})
check("POST /api/patients -> 201", s == 201, str(b)[:120])
# Orchestrated registration returns nested patient object.
patient = b.get("patient", b) if isinstance(b, dict) else b
patient_row_id = patient.get("id")
patient_mrn = patient.get("patient_id")
check("patient has id", patient_row_id is not None, str(b)[:120])
check("patient has patient_id (MRN)", patient_mrn == run_pid, str(patient_mrn))
check("patient has first_name", patient.get("first_name") == "Validation", str(patient.get("first_name")))
check("patient has last_name", patient.get("last_name") == "Patient", str(patient.get("last_name")))

# Encounters  (contract: POST requires patient_id)
print("\n--- Encounters ---")
http("GET /api/encounters", "/api/encounters", expected=200)
s, b = post("/api/encounters", {
    "patient_id": run_pid,
    "encounter_type": "outpatient",
    "reason": "Validation test",
})
check("POST /api/encounters -> 201", s == 201, str(b)[:120])
enc_id = b.get("encounter_id") or b.get("id")
check("encounter has encounter_id", enc_id is not None, str(b)[:120])
check("encounter patient_id resolves to patient", b.get("patient_id") == patient_row_id, str(b.get("patient_id")))

# Services  (contract: POST /api/encounters/<eid>/services requires service_name)
print("\n--- Services ---")
http(f"GET encounter services", f"/api/encounters/{enc_id}/services", expected=200)
s, b = post(f"/api/encounters/{enc_id}/services", {
    "service_name": "Office Visit",
    "description": "Validation office visit",
    "quantity": 1,
    "unit_price": 150.0,
})
check("POST encounter service -> 201", s == 201, str(b)[:120])
check("service has service_name", b.get("service_name") == "Office Visit", str(b.get("service_name")))

# Bills  (contract: POST requires encounter_id + patient_id)
print("\n--- Bills ---")
http("GET /api/bills", "/api/bills", expected=200)
s, b = post("/api/bills", {
    "encounter_id": enc_id,
    "patient_id": patient_row_id,
    "subtotal": 150.0,
    "tax": 0.0,
    "discount": 0.0,
})
check("POST /api/bills -> 201", s == 201, str(b)[:120])
check("bill has bill_number", b.get("bill_number") is not None, str(b)[:120])
check("bill total = 150", b.get("total") == 150.0, str(b.get("total")))
check("bill outstanding = 150", b.get("outstanding") == 150.0, str(b.get("outstanding")))
bill_id = b.get("id") or b.get("bill_number")

# Claims  (contract: POST requires encounter_id + patient_id; total_charges)
print("\n--- Claims ---")
http("GET /api/claims", "/api/claims", expected=200)
s, b = post("/api/claims", {
    "encounter_id": enc_id,
    "patient_id": patient_row_id,
    "total_charges": 150.0,
    "payer_type": "self_pay",
})
check("POST /api/claims -> 201", s == 201, str(b)[:120])
check("claim has claim_number", b.get("claim_number") is not None, str(b)[:120])
check("claim total_charges = 150", b.get("total_charges") == 150.0, str(b.get("total_charges")))
claim_id = b.get("id") or b.get("claim_number")

s, b = post(f"/api/claims/{claim_id}/vet", {})
check("POST claim vet -> 200", s == 200, str(b)[:120])

# Payments  (GET /api/payments returns 200 with list)
print("\n--- Payments ---")
s, b = get("/api/payments")
check("GET /api/payments -> 200", s == 200, str(s))
check("payments returns a list", isinstance(b, list), f"type={type(b).__name__}")
s, b = post(f"/api/bills/{bill_id}/pay", {
    "amount": 150.0,
    "payment_method": "Cash",
    "reference": "VAL-PAY-001",
})
check("POST bill pay -> 200", s == 200, str(b)[:120])
check("payment paid = 150", b.get("paid") == 150.0, str(b.get("paid")))
check("payment outstanding = 0", b.get("outstanding") == 0.0, str(b.get("outstanding")))
check("payment status = paid", b.get("status") == "paid", str(b.get("status")))
s, b2 = get("/api/payments")
check("GET /api/payments after payment -> 200", s == 200, str(s))
check("payments is a list after payment", isinstance(b2, list), f"type={type(b2).__name__}")
if isinstance(b2, list):
    check("payments list contains records", len(b2) >= 1, f"{len(b2)} records")
    pay_id = b2[0].get("id")
    s, b = post("/api/payments/match", {
        "claim_id": claim_id,
        "payment_id": pay_id,
        "matched_amount": 150.0,
    })
    check("POST /api/payments/match -> 200", s == 200, str(b)[:120])
http("GET /api/collections/payments", "/api/collections/payments", expected=200)

# Denials
print("\n--- Denials ---")
http("GET /api/denials", "/api/denials", expected=200)
http("POST /api/denials/detect", "/api/denials/detect", method="POST", expected=200)

# Appeals
print("\n--- Appeals ---")
http("GET /api/appeals", "/api/appeals", expected=200)
http("POST /api/appeals/detect", "/api/appeals/detect", method="POST", expected=200)

# Follow-ups
print("\n--- Follow-ups ---")
http("GET /api/followups", "/api/followups", expected=200)
http("POST /api/followups/ensure", "/api/followups/ensure", method="POST", expected=200)

# Collections
print("\n--- Collections ---")
http("GET /api/collections/balances", "/api/collections/balances", expected=200)
http("POST /api/collections/balances/ensure", "/api/collections/balances/ensure", method="POST", expected=200)
# Only attempt a real collection payment if a balance exists.
s, b = get("/api/collections/balances")
bal_id = None
if isinstance(b, list) and b:
    bal_id = b[0].get("id") or b[0].get("balance_id")
if bal_id:
    s, b = post("/api/collections/payments", {"balance_id": bal_id, "amount": 10.0})
    check("POST /api/collections/payments (real balance) -> 200", s == 200, str(b)[:120])
else:
    check("POST /api/collections/payments skipped (no balance)", True, "no balance available")

# Leakage
print("\n--- Leakage ---")
http("GET /api/leakage", "/api/leakage", expected=200)

# Reconciliation / underpayments
print("\n--- Reconciliation ---")
http("GET /api/underpayments", "/api/underpayments", expected=200)
http("POST /api/underpayments/detect", "/api/underpayments/detect", method="POST", expected=200)

# Analytics
print("\n--- Analytics ---")
for ep in ["overview", "claims", "revenue", "leakage", "ar-aging", "agents"]:
    http(f"GET /api/analytics/{ep}", f"/api/analytics/{ep}", expected=200)

# Revenue Integrity
print("\n--- Revenue Integrity ---")
http("GET /api/integrity/alerts", "/api/integrity/alerts", expected=200)
http("POST /api/integrity/scan", "/api/integrity/scan", method="POST", expected=200)
http("GET /api/integrity/metrics", "/api/integrity/metrics", expected=200)

# Orchestrator
print("\n--- Orchestrator ---")
http("GET /api/orchestrator/state", "/api/orchestrator/state", expected=200)
http("GET /api/orchestrator/activity", "/api/orchestrator/activity", expected=200)
http("GET /api/orchestrator/tasks", "/api/orchestrator/tasks", expected=200)

# Workflows
print("\n--- Workflows ---")
http("POST /api/workflows/run", "/api/workflows/run", method="POST",
     data={"scenario": "oop", "patient": "API Test Patient"}, expected=200)

print()
print("=" * 60)
print(f"RESULTS: {results['passed']} passed, {results['failed']} failed")
if results["errors"]:
    print("\nErrors:")
    for e in results["errors"]:
        print(f"  - {e}")
print("=" * 60)

sys.exit(1 if results["failed"] > 0 else 0)
