import app, json
from agents.denial import detect_denials, list_denials, denial_patterns, resolve_denial
from db import get_db, now
import uuid

with app.app.test_request_context():
    c = get_db()
    c.execute("DELETE FROM claims WHERE claim_number='CLM-TEST-DENY'")
    cid = str(uuid.uuid4())
    c.execute(
        "INSERT INTO claims(id, claim_number, encounter_id, patient_id, payer_type, payer_name, claim_type, diagnosis_codes, procedure_codes, total_charges, status, denial_reason, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "CLM-TEST-DENY", "79be3e88-8ed8-484c-9b2f-0f625af26ec2", "bea4deec-2773-434b-844d-73b12d5e46d4", "nhis", "NHIS Demo", "professional", '["J06.9"]', '["99213"]', 100.0, "denied", "Missing documentation to support services billed", now(), now()),
    )
    c.commit()
    c.close()

    r = detect_denials()
    print("detected denials:", len(r))
    for d in r:
        print(" -", d["claim_number"], d["denial_category"], d["status"])

    lst = list_denials()
    print("listed denials:", len(lst))

    pat = denial_patterns()
    print("patterns:", json.dumps(pat, indent=2)[:800])

    if lst:
        res = resolve_denial(lst[0]["id"], "corrected", "Added missing documentation")
        print("resolve:", res)
