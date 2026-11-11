import sqlite3
from db import get_db, now
import uuid

c = get_db()
claims = c.execute("SELECT id, claim_number, status, denial_reason FROM claims").fetchall()
print("All claims:")
for r in claims:
    print(" ", dict(r))

cid = str(uuid.uuid4())
try:
    c.execute(
        "INSERT INTO claims(id, claim_number, encounter_id, patient_id, payer_type, payer_name, claim_type, diagnosis_codes, procedure_codes, total_charges, status, denial_reason, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'denied', ?, ?)",
        (cid, "CLM-TEST-DENY2", "79be3e88-8ed8-484c-9b2f-0f625af26ec2", "bea4deec-2773-434b-844d-73b12d5e46d4", "nhis", "NHIS Demo", "professional", '["J06.9"]', '["99213"]', 100.0, now(), "Missing documentation to support services billed", now()),
    )
    c.commit()
    print("Inserted claim:", cid)
except Exception as e:
    print("Insert error:", e)

claims = c.execute("SELECT id, claim_number, status, denial_reason FROM claims WHERE claim_number='CLM-TEST-DENY2'").fetchall()
print("After insert:", [dict(r) for r in claims])
c.close()
