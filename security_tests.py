"""PNRCM Phase-3 security tests: RBAC + facility isolation (synthetic).

Sets up its own users/patients directly in the DB (WAL-safe), exercises the
API, and cleans up after itself. Run with the server up.
"""
import sqlite3, urllib.request, json, uuid, sys
from db import DB, now

BASE = 'http://localhost:5000'
_results = []


def call(path, method='GET', data=None, user=None):
    body = json.dumps(data if data is not None else {}).encode()
    headers = {'Content-Type': 'application/json'}
    if user:
        headers['X-User'] = user
    req = urllib.request.Request(BASE + path, data=body if method != 'GET' else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def check(name, cond, detail=''):
    _results.append((name, bool(cond), detail))
    print(('  [PASS] ' if cond else '  [FAIL] ') + name + ((' :: ' + str(detail)) if (detail and not cond) else ''))


# ---- Setup: two facilities, one facility-scoped user, patients in each ----
db = sqlite3.connect(DB, timeout=30)
db.row_factory = sqlite3.Row
tag = str(uuid.uuid4())[:8]
facA, facB = 'FACL-A-' + tag, 'FACL-B-' + tag
userA = 'fac_a_' + tag
patA = 'PATA-' + tag
patB = 'PATB-' + tag
try:
    db.execute("INSERT INTO patients(id, patient_id, first_name, last_name, facility_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?)".replace('?', '?'),
               (str(uuid.uuid4()), patA, 'FacA', 'Pat', facA, now(), now()))
    db.execute("INSERT INTO patients(id, patient_id, first_name, last_name, facility_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
               (str(uuid.uuid4()), patB, 'FacB', 'Pat', facB, now(), now()))
    db.execute("INSERT OR REPLACE INTO user_roles(id, username, role, facility_id, is_active, created_at) VALUES(?,?,?,?,1,?)",
               (str(uuid.uuid4()), userA, 'registration_officer', facA, now()))
    db.commit()
except Exception as e:
    print('setup error', e)

print('=== RBAC ===')
s, me = call('/api/auth/me', user='fin_off')
check('auth/me identifies finance officer', me.get('role') == 'finance_officer', me)
s, r = call('/api/payment/simulated', 'POST', {'claim_id': 'x', 'amount': 10}, user='doc_reviewer')
check('doc_reviewer blocked from payment (403)', s == 403, (s, r))
s, r = call('/api/users', 'POST', {'username': 'x'}, user='bill_off')
check('billing_officer blocked from admin (403)', s == 403, (s, r))
s, r = call('/api/users', 'GET', user='admin')
check('admin allowed admin read (200)', s == 200, s)
s, r = call('/api/patients', 'GET')  # anonymous
check('anonymous allowed (demo) (200)', s == 200, s)
s, r = call('/api/payment/simulated', 'POST', {'claim_id': 'x', 'amount': 10}, user='unknown_user')
check('unknown user passes RBAC (not 403)', s != 403, (s, r))

print('=== FACILITY ISOLATION ===')
s, pats = call('/api/patients', 'GET', user=userA)
ids = [p['patient_id'] for p in pats]
check('facility user sees own facility patient', patA in ids, ids[:5])
check('facility user does NOT see other facility patient', patB not in ids, 'leaked ' + patB)
s, allp = call('/api/patients', 'GET')  # anonymous sees all
allids = [p['patient_id'] for p in allp]
check('anonymous sees all facilities', patA in allids and patB in allids, 'missing')

# ---- Cleanup ----
try:
    db.execute("DELETE FROM patients WHERE patient_id IN (?,?)", (patA, patB))
    db.execute("DELETE FROM user_roles WHERE username=?", (userA,))
    db.commit()
except Exception as e:
    print('cleanup error', e)
db.close()

print()
passed = sum(1 for _, c, _ in _results if c)
total = len(_results)
print(f'=== SECURITY TEST RESULTS: {passed}/{total} passed ===')
if passed != total:
    for n, c, d in _results:
        if not c:
            print('  FAIL:', n, d)
    sys.exit(1)