import sqlite3
import sys

con = sqlite3.connect(r"storage\sessions.db")
con.row_factory = sqlite3.Row
cur = con.cursor()
sid = int(sys.argv[1]) if len(sys.argv) > 1 else 477
rows = cur.execute(
    "select id, seq, kind, tool, detail, evidence from events where session_id=? order by seq", (sid,)
).fetchall()
for r in rows:
    print(r["id"], r["seq"], r["kind"], r["tool"], "|", (r["detail"] or "")[:80], "|", r["evidence"])
