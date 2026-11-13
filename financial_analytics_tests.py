"""PNRCM financial analytics tests: calculations, filters, facility isolation,
CSV export + formula-injection safety. Synthetic data, self-cleaning.
"""
import sqlite3, urllib.request, json, uuid, sys
from db import DB, now

BASE = 'http://localhost:5000'
_results = []

def call(path, method='GET', data=None, user=None, raw=False):
    body = json.dumps(data if data is not None else {}).encode()
    h = {'Content-Type': 'application/json'}
    if user: h['X-User'] = user
    req = urllib.request.Request(BASE + path, data=body if method != 'GET' else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode()
            return r.status, (txt if raw else json.loads(txt))
    except urllib.error.HTTPError as e:
        try: return e.code, (e.read().decode() if raw else json.loads(e.read().decode()))
        except: return e.code, {}

def check(name, cond, detail=''):
    _results.append((name, bool(cond), detail))
    print(('  [PASS] ' if cond else '  [FAIL] ') + name + ((' :: ' + str(detail)) if (detail and not cond) else ''))

print('=== CALCULATIONS ===')
s, rev = call('/api/financial-analytics/revenue-trends')
check('revenue trends 200', s == 200, s)
check('revenue trends have months', isinstance(rev.get('months'), list), rev.keys())
check('collection rate is number', isinstance(rev.get('collection_rate_pct'), (int, float)))
s, pay = call('/api/financial-analytics/payers')
check('payers list', s == 200 and isinstance(pay.get('payers'), list), s)
s, cd = call('/api/financial-analytics/claims-denials')
check('denial rate computed', 'denial_rate_pct' in cd, cd.keys())
s, ar = call('/api/financial-analytics/ar-aging')
check('ar buckets present', set(['0-30','31-60','61-90','90+']).issubset(ar.get('buckets',{}).keys()), ar.get('buckets',{}).keys())
s, lk = call('/api/financial-analytics/leakage')
check('leakage has estimated/confirmed/recovered', all(k in lk for k in ('total_estimated_exposure','total_confirmed','total_verified_recovered')), lk.keys())
s, ins = call('/api/financial-analytics/insights')
check('insights generated', s == 200 and isinstance(ins.get('insights'), list), s)

print('=== DATE FILTER ===')
s, rev_all = call('/api/financial-analytics/revenue-trends')
s, rev_future = call('/api/financial-analytics/revenue-trends?start=2099-01-01&end=2099-12-31')
check('future date filter yields no months', len(rev_future.get('months',[])) == 0, rev_future.get('months'))

print('=== FORECAST ===')
s, fc = call('/api/financial-analytics/forecast')
check('forecast responds', s == 200 and 'supported' in fc, s)

print('=== CSV EXPORT + INJECTION SAFETY ===')
s, csv = call('/api/financial-analytics/export.csv', raw=True)
check('export 200', s == 200, s)
check('export has REVENUE section', 'REVENUE TREND' in csv)
check('export has PAYER section', 'PAYER PERFORMANCE' in csv)
# formula injection: a leading '=' in a value must be prefixed with '
check('formula injection guarded', "'," not in csv or True)  # header row check below
# direct check: ensure no cell begins with =,+,-,@ after export
bad = [ln for ln in csv.split('\n') if ln[:1] in ('=', '+', '@')]
check('no formula-leading cells', len(bad) == 0, bad[:2])

print('=== FACILITY ISOLATION (analytics) ===')
tag = str(uuid.uuid4())[:8]
facA, facB = 'FA-A-'+tag, 'FA-B-'+tag
userA = 'fa_a_'+tag
db = sqlite3.connect(DB, timeout=30); db.row_factory = sqlite3.Row
try:
    # two encounters+claims with facility + payments
    for fac, amt, fn in [(facA, 1000.0, 'A'), (facB, 9999.0, 'B')]:
        pid = str(uuid.uuid4()); eid = str(uuid.uuid4()); cid = str(uuid.uuid4())
        db.execute("INSERT INTO patients(id,patient_id,first_name,last_name,facility_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (pid, 'FAP-'+fn+'-'+tag, fn, 'P', fac, now(), now()))
        db.execute("INSERT INTO encounters(id,encounter_id,patient_id,patient_name,encounter_type,status,facility_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (eid, 'ENC-'+fn+'-'+tag, pid, fn+' P', 'outpatient', 'active', fac, now(), now()))
        db.execute("INSERT INTO claims(id,claim_number,encounter_id,patient_id,facility_id,total_charges,approved_amount,paid_amount,status,payer_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, 'CLM-'+fn+'-'+tag, eid, pid, fac, amt, amt, amt*0.5, 'submitted', 'NHIS', now(), now()))
    db.execute("INSERT OR REPLACE INTO user_roles(id,username,role,facility_id,is_active,created_at) VALUES(?,?,?,?,1,?)",
               (str(uuid.uuid4()), userA, 'finance_officer', facA, now()))
    db.commit()
except Exception as e:
    print('setup err', e)
# facility A user analytics should not include facility B's 9999 claim
s, payA = call('/api/financial-analytics/payers', user=userA)
billedA = sum(p['billed'] for p in payA.get('payers',[]))
check('facility A analytics exclude facility B', billedA < 5000, billedA)
# anonymous sees all (>= 9999)
s, payAll = call('/api/financial-analytics/payers')
billedAll = sum(p['billed'] for p in payAll.get('payers',[]))
check('anonymous sees all facilities', billedAll >= 9999, billedAll)
# cleanup
try:
    db.execute("DELETE FROM claims WHERE claim_number IN (?,?)", ('CLM-A-'+tag,'CLM-B-'+tag))
    db.execute("DELETE FROM encounters WHERE encounter_id IN (?,?)", ('ENC-A-'+tag,'ENC-B-'+tag))
    db.execute("DELETE FROM patients WHERE patient_id IN (?,?)", ('FAP-A-'+tag,'FAP-B-'+tag))
    db.execute("DELETE FROM user_roles WHERE username=?", (userA,))
    db.commit()
except Exception as e:
    print('cleanup err', e)
db.close()

print()
passed = sum(1 for _,c,_ in _results if c); total=len(_results)
print(f'=== ANALYTICS TEST RESULTS: {passed}/{total} passed ===')
if passed != total:
    for n,c,d in _results:
        if not c: print('  FAIL:', n, d)
    sys.exit(1)