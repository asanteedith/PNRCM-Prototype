import sqlite3
c = sqlite3.connect("pnrcm_demo.db")
c.execute("DELETE FROM denials")
c.execute("DELETE FROM claims WHERE claim_number='CLM-TEST-DENY'")
c.commit()
print("denials:", c.execute("SELECT COUNT(*) FROM denials").fetchone()[0])
print("test claims:", c.execute("SELECT COUNT(*) FROM claims WHERE claim_number='CLM-TEST-DENY'").fetchone()[0])
c.close()
