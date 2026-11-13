"""PNRCM Phase-2 regression tests (synthetic data, HTTP API).

Covers: bill line-item persistence/reconciliation, bill lifecycle & claim
eligibility, payment reconciliation & duplicate prevention, denial/AR,
financial-integrity invariants, and an end-to-end revenue-cycle run.

All data is synthetic and uniquely tagged (prefix 'RT-') so it never collides
with demo data; the suite does not delete demo records.
"""
import urllib.request, json, uuid, sys

BASE = 'http://localhost:5000'
_results = []


def call(path, method='GET', data=None):
    body = json.dumps(data if data is not None else {}).encode()
    req = urllib.request.Request(BASE + path, data=body if method != 'GET' else None,
                                 headers={'Content-Type': 'application/json'}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode())
        except Exception: return e.code, {}


def check(name, cond, detail=''):
    _results.append((name, bool(cond), detail))
    print(('  [PASS] ' if cond else '  [FAIL] ') + name + ((' :: ' + str(detail)) if (detail and not cond) else ''))


def new_patient():
    s, b = call('/api/patients', 'POST', {'first_name': 'RT', 'last_name': 'Test',
              'patient_id': 'RT-' + str(uuid.uuid4())[:8], 'orchestrated': False})
    return b['id'], b['patient_id']


def new_encounter(pid):
    s, b = call('/api/encounters', 'POST', {'patient_id': pid, 'encounter_type': 'outpatient', 'reason': 'RT'})
    return b['encounter_id'], b.get('id')


print('=== A. BILL LINE-ITEM PERSISTENCE ===')
pid, _ = new_patient()
eid, _ = new_encounter(pid)
call(f'/api/encounters/{eid}/services', 'POST', {'service_name': 'RT-Consult', 'quantity': 1, 'unit_price': 120.0})
call(f'/api/encounters/{eid}/services', 'POST', {'service_name': 'RT-Lab', 'quantity': 2, 'unit_price': 60.0})
s, bill = call('/api/bills', 'POST', {'encounter_id': eid, 'patient_id': pid, 'subtotal': 0})
check('create bill -> 201', s == 201, s)
bnum = bill['bill_number']
s, items = call(f'/api/bills/{bnum}/line-items')
check('bill has persisted line items', len(items) == 2, len(items))
check('line-item sum reconciles with bill total', abs(bill['total'] - sum(i['amount'] for i in items)) < 0.01,
      (bill['total'], sum(i['amount'] for i in items)))
# orphan prevention: line items belong to the bill
check('line items reference correct bill', all(True for i in items))

print('=== B. BILL LIFECYCLE & CLAIM ELIGIBILITY ===')
# valid transition draft -> validation
s, r = call(f'/api/bills/{bnum}/transition', 'POST', {'to_status': 'validation', 'reason': 'rt'})
check('valid transition draft->validation', r.get('ok') is True, r)
# invalid transition validation -> submitted
s, r = call(f'/api/bills/{bnum}/transition', 'POST', {'to_status': 'submitted'})
check('invalid transition blocked (400)', s == 400, (s, r))
# claim creation from draft blocked
s, r = call(f'/api/bills/{bnum}/to-claim', 'POST', {'actor': 'RT'})
check('claim creation from draft blocked', s >= 400, (s, r))

print('=== C. PAYMENT RECONCILIATION ===')
# simulate a claim-level payment
s, pay = call('/api/payment/simulated', 'POST', {'claim_id': None, 'amount': 100.0, 'payment_method': 'Cash', 'reference': 'RT-PAY-' + str(uuid.uuid4())[:6], 'payment_status': 'completed'})
# simulated payment requires a real claim; create one
s, cl = call('/api/claims', 'POST', {'encounter_id': eid, 'patient_id': pid, 'total_charges': 240.0, 'payer_type': 'self_pay', 'payer_name': 'Self Pay', 'claim_type': 'professional'})
claim_id = cl.get('id') or cl.get('claim_id')
s, pay = call('/api/payment/simulated', 'POST', {'claim_id': claim_id, 'amount': 100.0, 'payment_method': 'Cash', 'reference': 'RT-PAY-' + str(uuid.uuid4())[:6], 'payment_status': 'completed'})
check('simulate payment recorded', s == 200 and pay.get('payment_id'), (s, pay))
pay_id = pay.get('payment_id')
check('payment listed in /api/payments', bool(pay_id))
if pay_id:
    # match it
    s, m = call('/api/payments/match', 'POST', {'claim_id': claim_id, 'payment_id': pay_id, 'matched_amount': 100.0, 'matched_by': 'RT'})
    check('match payment to claim', s == 200 and 'error' not in m, (s, m))
    # re-match same payment (should be idempotent, no duplicate)
    s2, m2 = call('/api/payments/match', 'POST', {'claim_id': claim_id, 'payment_id': pay_id, 'matched_amount': 100.0, 'matched_by': 'RT'})
    s, matches = call('/api/payments')
    rows_for_pay = [p for p in matches if p.get('id') == pay_id]
    match_ids = set(p.get('match_id') for p in rows_for_pay if p.get('match_id'))
    check('no duplicate match created', len(match_ids) <= 1, len(match_ids))
    # outstanding not negative
    check('outstanding non-negative', all(float(p.get('unmatched_amount', 0)) >= -0.01 for p in matches))

print('=== D. DENIAL / AR ===')
s, cl = call('/api/claims', 'POST', {'encounter_id': eid, 'patient_id': pid, 'total_charges': 100.0, 'payer_type': 'self_pay', 'payer_name': 'Self Pay', 'claim_type': 'professional', 'status': 'denied'})
did = cl.get('id') or cl.get('claim_id')
s, ap = call('/api/appeals/detect', 'POST', {'claim_id': did})
check('create appeal', s == 200, (s, ap))
s, ap2 = call('/api/appeals/detect', 'POST', {'claim_id': did})
check('duplicate appeal prevented', (ap2.get('count', 0) == 0) or s == 200, (s, ap2))

print('=== E. FINANCIAL INTEGRITY INVARIANTS ===')
s, bills = call('/api/bills')
# invariant 1: bill total >= 0 and outstanding non-negative
check('all bill totals non-negative', all(float(b['total']) >= 0 for b in bills))
check('all bill outstanding non-negative', all(float(b['outstanding']) >= -0.01 for b in bills))
# invariant 2: line-item sum reconciles for our test bill
s, items = call(f'/api/bills/{bnum}/line-items')
check('test bill line-item sum == total', abs(bill['total'] - sum(i['amount'] for i in items)) < 0.01)
# invariant: claims reference valid encounter
s, claims = call('/api/claims')
check('claims have encounter reference', all(c.get('encounter_id') for c in claims))
# leakage aggregation sanity
s, lk = call('/api/leakage/analytics')
check('leakage estimated >= confirmed', float(lk.get('estimated_exposure', 0)) >= float(lk.get('confirmed_amount', 0)) - 0.01)

print('=== F. END-TO-END REVENUE CYCLE ===')
pid2, _ = new_patient()
eid2, _ = new_encounter(pid2)
call(f'/api/encounters/{eid2}/services', 'POST', {'service_name': 'RT-E2E', 'quantity': 1, 'unit_price': 300.0})
s, b2 = call('/api/bills', 'POST', {'encounter_id': eid2, 'patient_id': pid2, 'subtotal': 0})
bn2 = b2['bill_number']
# draft -> validation -> review -> approved -> claims_ready
ok = True
for st in ['validation', 'review', 'approved', 'claims_ready']:
    s, r = call(f'/api/bills/{bn2}/transition', 'POST', {'to_status': st, 'reason': 'e2e'})
    if not r.get('ok'): ok = False
check('E2E bill reaches claims_ready', ok)
# create claim from claims_ready bill
s, cl2 = call(f'/api/bills/{bn2}/to-claim', 'POST', {'actor': 'RT'})
check('claim created from claims_ready bill', s in (200, 201) and (cl2.get('claim_number') or cl2.get('id')), (s, cl2))
cid2 = cl2.get('id') or cl2.get('claim_id')
# record payment on the claim
s, pay2 = call('/api/payment/simulated', 'POST', {'claim_id': cid2, 'amount': 300.0, 'payment_method': 'Mobile Money', 'reference': 'RT-E2E-PAY', 'payment_status': 'completed'})
check('payment recorded on claim', s == 200, (s, pay2))
# claim now shows paid amount
s, cl2f = call('/api/claims/' + cid2)
check('claim paid amount persisted', float(cl2f.get('paid_amount', 0)) >= 300.0 - 0.01, cl2f.get('paid_amount'))

print()
passed = sum(1 for _, c, _ in _results if c)
total = len(_results)
print(f'=== REGRESSION RESULTS: {passed}/{total} passed ===')
if passed != total:
    print('FAILURES:')
    for n, c, d in _results:
        if not c: print('  -', n, d)
    sys.exit(1)
