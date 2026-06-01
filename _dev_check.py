import sqlite3
for p in ("dev.db", "dazebot.db"):
    try:
        c = sqlite3.connect(p)
        print(p, [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")])
    except Exception as e:
        print(p, "ERR", e)
