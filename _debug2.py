from db import get_db
c = get_db()
claims = c.execute("SELECT * FROM claims WHERE status IN ('denied','rejected')").fetchall()
print("denied/rejected claims:", len(claims))
for r in claims:
    print(" ", dict(r))
c.close()
