# migrate_subscriptions_schema_fix.py
# Ensures subscriptions table has the expected columns/defaults (idempotent).

from sqlalchemy import text
from db import session_scope

SQL = """
-- Add missing columns if any
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS provider_sub_id TEXT;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS status_internal TEXT;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

-- Defaults for timestamps
ALTER TABLE subscriptions ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE subscriptions ALTER COLUMN updated_at SET DEFAULT NOW();

-- Backfill NULLs
UPDATE subscriptions SET created_at = NOW() WHERE created_at IS NULL;
UPDATE subscriptions SET updated_at = NOW() WHERE updated_at IS NULL;

-- NOT NULL for timestamps
ALTER TABLE subscriptions ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE subscriptions ALTER COLUMN updated_at SET NOT NULL;

-- Basic defaults
UPDATE subscriptions SET provider = COALESCE(provider, 'paypal');
"""

CHECKS = """
SELECT
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='subscriptions' AND column_name='provider_sub_id') AS has_provider_sub_id;
"""

if __name__ == "__main__":
    print("[migrate subscriptions schema fix] starting…")
    with session_scope() as s:
        s.execute(text(SQL))
        row = s.execute(text(CHECKS)).first()
        print(f"[migrate subscriptions schema fix] has_provider_sub_id={row.has_provider_sub_id}")
    print("[migrate subscriptions schema fix] ok ✅")
