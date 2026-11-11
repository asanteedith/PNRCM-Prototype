import sqlite3
c = sqlite3.connect("pnrcm_demo.db")
c.execute("DELETE FROM claims WHERE claim_number='CLM-TEST-DENY'")
c.commit()
c.close()
print("cleaned")
