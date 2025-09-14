# migrate_alerts_enabled_default.py
from sqlalchemy import text
from db import session_scope

SQL = """
-- 1) Set default TRUE
ALTER TABLE alerts ALTER COLUMN enabled SET DEFAULT TRUE;

-- 2) Backfill NULLs to TRUE
UPDATE alerts SET enabled = TRUE WHERE enabled IS NULL;

-- 3) Enforce NOT NULL
ALTER TABLE alerts ALTER COLUMN enabled SET NOT NULL;
"""

if __name__ == "__main__":
    print("[migrate alerts.enabled default] starting…")
    with session_scope() as s:
        s.execute(text(SQL))
        row = s.execute(text("SELECT COUNT(*) FROM alerts WHERE enabled IS NULL")).first()
        print(f"[migrate alerts.enabled default] null_enabled={row[0]}")
    print("[migrate alerts.enabled default] ok ✅")
