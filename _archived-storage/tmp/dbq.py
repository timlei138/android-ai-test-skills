import sqlite3
import sys

con = sqlite3.connect(r"storage\sessions.db")
con.row_factory = sqlite3.Row
cur = con.cursor()
sid = sys.argv[1] if len(sys.argv) > 1 else "477"
tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'")]
print("tables:", tables)
cur.execute("PRAGMA table_info(events)")
print([r[1] for r in cur.fetchall()])
