from db import get_db, now
import uuid

c = get_db()
# Remove bad test claims
c.execute("DELETE FROM claims WHERE claim_number IN ('CLM-TEST-DENY', 'CLM-TEST-DENY2')")
# Insert a properly structured denied claim
cid = str(uuid.uuid4())
c.execute(
    "INSERT INTO claims(id, claim_number, encounter_id, patient_id, payer_type, payer_name, claim_type, diagnosis_codes, procedure_codes, total_charges, status, denial_reason, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (cid, "CLM-TEST-DENY", "79be3e88-8ed8-484c-9b2f-0f625af26ec2", "bea4deec-2773-434b-844d-73b12d5e46d4", "nhis", "NHIS Demo", "professional", '["J06.9"]', '["99213"]', 100.0, "denied", "Missing documentation to support services billed", now(), now()),
)
c.commit()
# Verify
r = c.execute("SELECT claim_number, status, denial_reason FROM claims WHERE claim_number='CLM-TEST-DENY'").fetchone()
print("Verified:", dict(r))
c.close()
