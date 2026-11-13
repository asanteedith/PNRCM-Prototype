"""PNRCM Phase-4 facility isolation tests for financial records.

Seeds facility-scoped encounters/patients/bills directly, verifies that a
facility-scoped user only sees their facility's bills/claims and that
cross-facility matching is rejected. Cleans up after itself.
"""
import sqlite3, urllib.request, json, uuid, sys
from db import DB, now

BASE = 'http://localhost:5000'
_results = []

def call(path, method='GET', data=None, user=None):
    body = json.dumps(data if data is not None else {}).encode()
    h = {'Content-Type': 'application/json'}
    if user: h['X-User'] = user
    req = urllib.request.Request(BASE + path, data=body if method != 'GET' else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode())
        except: return e.code, {}

def check(name, cond, detail=''):
    _results.append((name, bool(cond), detail))
    print(('  [PASS] ' if cond else '  [FAIL] ') + name + ((' :: ' + str(detail)) if (detail and not cond) else ''))

tag = str(uuid.uuid4())[:8]
facA, facB = 'F4-A-' + tag, 'F4-B-' + tag
userA = 'f4_a_' + tag
patA, patB = 'F4PATA-' + tag, 'F4PATB-' + tag
encA, encB = str(uuid.uuid4()), str(uuid.uuid4())

db = sqlite3.connect(DB, timeout=30); db.row_factory = sqlite3.Row
try:
    for pid, fac, fn in [(patA, facA, 'A'), (patB, facB, 'B')]:
        db.execute("INSERT INTO patients(id, patient_id, first_name, last_name, facility_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                   (str(uuid.uuid4()), pid, 'Fac'+fn, 'Pat', fac, now(), now()))
    db.execute("INSERT INTO encounters(id, encounter_id, patient_id, patient_name, encounter_type, status, facility_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
               (encA, 'ENC-A-'+tag, patA, 'FacA Pat', 'outpatient', 'active', facA, now(), now()))
    db.execute("INSERT INTO encounters(id, encounter_id, patient_id, patient_name, encounter_type, status, facility_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
               (encB, 'ENC-B-'+tag, patB, 'FacB Pat', 'outpatient', 'active', facB, now(), now()))
    db.execute("INSERT OR REPLACE INTO user_roles(id, username, role, facility_id, is_active, created_at) VALUES(?,?,?,?,1,?)",
               (str(uuid.uuid4()), userA, 'billing_officer', facA, now()))
    db.commit()
except Exception as e:
    print('setup error', e)

print('=== FACILITY SCOPING (bills/claims) ===')
# create bills for each encounter (inherit facility from encounter)
s, bA = call('/api/bills', 'POST', {'encounter_id': encA, 'patient_id': patA, 'subtotal': 100.0}, user=userA)
s, bB = call('/api/bills', 'POST', {'encounter_id': encB, 'patient_id': patB, 'subtotal': 200.0}, user=userA)
check('bill A created', s == 201, (s, bA))
check('bill A has facility A', bA.get('facility_id') == facA, bA.get('facility_id'))

# facility A user lists bills -> only A
s, bills = call('/api/bills', 'GET', user=userA)
fac_ids = set(b.get('facility_id') for b in bills)
check('facility user sees only own-facility bills', facA in fac_ids and facB not in fac_ids, fac_ids)

# anonymous sees all (demo)
s, allb = call('/api/bills', 'GET')
all_ids = [b.get('bill_number') for b in allb]
check('anonymous sees all bills', bA.get('bill_number') in all_ids and bB.get('bill_number') in all_ids, 'missing')

print('=== CROSS-FACILITY MATCH PREVENTION ===')
# create claims in each facility, then a payment in A, try to match to B claim
s, clA = call('/api/claims', 'POST', {'encounter_id': encA, 'patient_id': patA, 'total_charges': 100.0, 'payer_type': 'self_pay', 'claim_type': 'professional'}, user=userA)
s, clB = call('/api/claims', 'POST', {'encounter_id': encB, 'patient_id': patB, 'total_charges': 200.0, 'payer_type': 'self_pay', 'claim_type': 'professional'}, user=userA)
cidA = clA.get('id'); cidB = clB.get('id')
# manually set payment facility to B and try to match to claim A -> should be rejected
pay_id = str(uuid.uuid4())
try:
    db.execute("INSERT INTO payments(id, claim_id, facility_id, amount, payment_method, status, created_at) VALUES(?,?,?,?,?,?,?)",
               (pay_id, cidB, facB, 50.0, 'Cash', 'completed', now()))
    db.commit()
except Exception as e:
    print('pay setup err', e)
s, m = call('/api/payments/match', 'POST', {'claim_id': cidA, 'payment_id': pay_id, 'matched_amount': 50.0}, user='admin')
check('cross-facility match rejected', s == 400, (s, m))

# cleanup
try:
    db.execute("DELETE FROM payments WHERE id=?", (pay_id,))
    db.execute("DELETE FROM claims WHERE encounter_id IN (?,?)", (encA, encB))
    db.execute("DELETE FROM bills WHERE encounter_id IN (?,?)", (encA, encB))
    db.execute("DELETE FROM encounters WHERE encounter_id IN (?,?)", ('ENC-A-'+tag, 'ENC-B-'+tag))
    db.execute("DELETE FROM patients WHERE patient_id IN (?,?)", (patA, patB))
    db.execute("DELETE FROM user_roles WHERE username=?", (userA,))
    db.commit()
except Exception as e:
    print('cleanup err', e)
db.close()

print()
passed = sum(1 for _, c, _ in _results if c); total = len(_results)
print(f'=== FACILITY TEST RESULTS: {passed}/{total} passed ===')
if passed != total:
    for n, c, d in _results:
        if not c: print('  FAIL:', n, d)
    sys.exit(1)