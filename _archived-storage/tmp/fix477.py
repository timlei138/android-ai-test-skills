import sqlite3
db = r"D:/Project/deepseek/android-ai-test-skills/storage/sessions.db"
con = sqlite3.connect(db)
reason = "主 Agent 收尾：会话中断，未产生任何断言（0 findings / 33 events），非功能结论"
con.execute("update sessions set terminate_reason=? where id=477", (reason,))
con.commit()
print(con.execute("select id,status,terminated,terminate_reason from sessions where id=477").fetchone())
con.close()
