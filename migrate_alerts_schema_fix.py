# migrate_alerts_schema_fix.py
from sqlalchemy import text
from db import session_scope

SQL = """
-- alerts timestamps
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;
ALTER TABLE alerts ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE alerts ALTER COLUMN updated_at SET DEFAULT NOW();
UPDATE alerts SET created_at = NOW() WHERE created_at IS NULL;
UPDATE alerts SET updated_at = NOW() WHERE updated_at IS NULL;
ALTER TABLE alerts ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE alerts ALTER COLUMN updated_at SET NOT NULL;

-- alerts.enabled: default/bakfill/not null
ALTER TABLE alerts ALTER COLUMN enabled SET DEFAULT TRUE;
UPDATE alerts SET enabled = TRUE WHERE enabled IS NULL;
ALTER TABLE alerts ALTER COLUMN enabled SET NOT NULL;

-- users timestamps (defensive)
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;
ALTER TABLE users ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE users ALTER COLUMN updated_at SET DEFAULT NOW();
UPDATE users SET created_at = NOW() WHERE created_at IS NULL;
UPDATE users SET updated_at = NOW() WHERE updated_at IS NULL;
ALTER TABLE users ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE users ALTER COLUMN updated_at SET NOT NULL;
"""

if __name__ == "__main__":
    print("[migrate alerts schema fix] starting…")
    with session_scope() as s:
        s.execute(text(SQL))
    print("[migrate alerts schema fix] ok ✅")
