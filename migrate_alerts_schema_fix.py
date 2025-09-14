# migrate_alerts_schema_fix.py
# Fixes NOT NULL/DEFAULT for alerts.created_at, alerts.updated_at, alerts.enabled (idempotent)
# Also ensures users.created_at/updated_at defaults (safe to run many times)

from sqlalchemy import text
from db import session_scope

SQL = """
-- Ensure columns exist (safe if already there)
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

-- Set defaults
ALTER TABLE alerts ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE alerts ALTER COLUMN updated_at SET DEFAULT NOW();

-- Backfill NULLs
UPDATE alerts SET created_at = NOW() WHERE created_at IS NULL;
UPDATE alerts SET updated_at = NOW() WHERE updated_at IS NULL;

-- Enforce NOT NULL
ALTER TABLE alerts ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE alerts ALTER COLUMN updated_at SET NOT NULL;

-- enabled: default + backfill + not null
ALTER TABLE alerts ALTER COLUMN enabled SET DEFAULT TRUE;
UPDATE alerts SET enabled = TRUE WHERE enabled IS NULL;
ALTER TABLE alerts ALTER COLUMN enabled SET NOT NULL;

-- Users timestamps (defensive hardening)
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

ALTER TABLE users ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE users ALTER COLUMN updated_at SET DEFAULT NOW();

UPDATE users SET created_at = NOW() WHERE created_at IS NULL;
UPDATE users SET updated_at = NOW() WHERE updated_at IS NULL;

ALTER TABLE users ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE users ALTER COLUMN updated_at SET NOT NULL;
"""

CHECKS = """
SELECT
  (SELECT COUNT(*) FROM alerts WHERE created_at IS NULL) AS a_created_nulls,
  (SELECT COUNT(*) FROM alerts WHERE updated_at IS NULL) AS a_updated_nulls,
  (SELECT COUNT(*) FROM alerts WHERE enabled   IS NULL) AS a_enabled_nulls,
  (SELECT COUNT(*) FROM users  WHERE created_at IS NULL) AS u_created_nulls,
  (SELECT COUNT(*) FROM users  WHERE updated_at IS NULL) AS u_updated_nulls;
"""

if __name__ == "__main__":
    print("[migrate alerts schema fix] starting…")
    with session_scope() as s:
        s.execute(text(SQL))
        row = s.execute(text(CHECKS)).first()
        print(
            "[migrate alerts schema fix] "
            f"alerts(created_nulls={row.a_created_nulls}, updated_nulls={row.a_updated_nulls}, enabled_nulls={row.a_enabled_nulls}); "
            f"users(created_nulls={row.u_created_nulls}, updated_nulls={row.u_updated_nulls})"
        )
    print("[migrate alerts schema fix] ok ✅")
