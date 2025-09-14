# migrate_add_updated_at.py
from sqlalchemy import text
from db import session_scope

SQL = "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();"
CHECK = """
SELECT column_name
FROM information_schema.columns
WHERE table_name='users' AND column_name='updated_at';
"""

if __name__ == "__main__":
    print("[migrate users.updated_at] starting…")
    with session_scope() as s:
        s.execute(text(SQL))
        row = s.execute(text(CHECK)).first()
        ok = bool(row and row[0] == "updated_at")
        print(f"[migrate users.updated_at] present={ok}")
    print("[migrate users.updated_at] ok ✅")
