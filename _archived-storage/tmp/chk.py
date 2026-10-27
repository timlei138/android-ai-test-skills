import sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8")
db = r"D:/Project/deepseek/android-ai-test-skills/storage/sessions.db"
con = sqlite3.connect(db)
v = con.execute("select terminate_reason from sessions where id=477").fetchone()[0]
print("REPR:", repr(v))
print("LEN:", len(v))
con.close()
