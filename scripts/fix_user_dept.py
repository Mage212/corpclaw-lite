"""Fix department references across all DBs for user 278278319."""
import sqlite3

# 1. users.db — verify
conn = sqlite3.connect("data/users.db")
row = conn.execute("SELECT id, telegram_id, name, department FROM users WHERE telegram_id=278278319").fetchone()
print(f"users.db -> {row}")
conn.close()

# 2. memory.db — memory_facts may contain old dept from onboarding
conn = sqlite3.connect("data/memory.db")
facts = conn.execute("SELECT id, key, value FROM memory_facts WHERE user_id='278278319'").fetchall()
print(f"memory_facts before: {facts}")
conn.execute(
    "UPDATE memory_facts SET value = 'admin' WHERE user_id = '278278319' AND key = 'department'"
)
conn.commit()
facts = conn.execute("SELECT id, key, value FROM memory_facts WHERE user_id='278278319'").fetchall()
print(f"memory_facts after: {facts}")
conn.close()
