"""PHRCM security hardening tests: headers, rate limiting, RBAC, CSRF rationale,
sensitive-data leakage, audit logging. Synthetic, no destructive changes.
"""
import urllib.request, json, uuid, sys
BASE = 'http://localhost:5000'
_results = []

def call(path, method='GET', data=None, user=None):
    body = json.dumps(data if data is not None else {}).encode()
    h = {'Content-Type': 'application/json'}
    if user: h['X-User'] = user
    req = urllib.request.Request(BASE + path, data=body if method != 'GET' else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        try: return e.code, dict(e.headers), e.read().decode()
        except: return e.code, {}, ''

def check(name, cond, detail=''):
    _results.append((name, bool(cond), detail))
    print(('  [PASS] ' if cond else '  [FAIL] ') + name + ((' :: ' + str(detail)) if (detail and not cond) else ''))

print('=== SECURITY HEADERS ===')
s, h, _ = call('/api/health')
check('X-Content-Type-Options=nosniff', h.get('X-Content-Type-Options') == 'nosniff', h.get('X-Content-Type-Options'))
check('X-Frame-Options=DENY', h.get('X-Frame-Options') == 'DENY', h.get('X-Frame-Options'))
check('Referrer-Policy set', bool(h.get('Referrer-Policy')), h.get('Referrer-Policy'))
check('Content-Security-Policy set', bool(h.get('Content-Security-Policy')))

print('=== RATE LIMITING ===')
import security
allowed = sum(1 for i in range(35) if security.check_rate_limit('9.9.9.9', '/api/financial-analytics/export.csv')[0])
check('export limiter caps at 30/min', allowed == 30, allowed)
# HTTP: a single export still works (not globally blocked)
s, h, b = call('/api/financial-analytics/export.csv')
check('export works under limit', s == 200, s)

print('=== RBAC (server-side) ===')
s, h, b = call('/api/payment/simulated', 'POST', {'claim_id':'x','amount':1}, user='doc_reviewer')
check('coding reviewer blocked from payment', s == 403, s)
s, h, b = call('/api/users', 'POST', {'username':'x'}, user='bill_off')
check('billing officer blocked from admin', s == 403, s)

print('=== SENSITIVE DATA LEAKAGE ===')
# error responses must not contain stack traces / secrets
s, h, b = call('/api/leakage/cases/does-not-exist')
check('404 has no traceback', 'Traceback' not in b and 'File "' not in b, b[:80])
s, h, b = call('/api/financial-analytics/export.csv')
check('export no password/secret', 'password' not in b.lower() and 'secret' not in b.lower(), 'leaked')

print('=== CSRF RATIONALE ===')
# PNRCM uses header-based auth (X-User), not cookies; no cookie set
s, h, b = call('/api/auth/me', user='admin')
check('no session cookie set (header auth)', 'set-cookie' not in {k.lower() for k in h.keys()}, list(h.keys()))

print('=== AUDIT LOGGING ===')
s, h, b = call('/api/audit')
ok = False
try:
    d = json.loads(b)
    ok = isinstance(d, list)
except: pass
check('audit endpoint returns records', ok, b[:60])

print()
passed = sum(1 for _,c,_ in _results if c); total=len(_results)
print(f'=== SECURITY TEST RESULTS: {passed}/{total} passed ===')
if passed != total:
    for n,c,d in _results:
        if not c: print('  FAIL:', n, d)
    sys.exit(1)