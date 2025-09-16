# migrate_user_settings.py
# Creates/fixes the `user_settings` table used by extra features (/pumplive, daily news, etc.)
# Idempotent: safe to run multiple times.

from sqlalchemy import text
from db import session_scope

SQL = """
-- 1) Create table if not exists
CREATE TABLE IF NOT EXISTS user_settings (
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    key        TEXT,
    value      TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 2) Add missing columns (if table exists but columns differ)
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS key        TEXT;
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS value      TEXT;
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

-- 3) Defaults/backfill
ALTER TABLE user_settings ALTER COLUMN updated_at SET DEFAULT NOW();
UPDATE user_settings SET updated_at = NOW() WHERE updated_at IS NULL;

-- 4) Ensure unique index on (user_id, key)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'ux_user_settings_user_key'
    ) THEN
        CREATE UNIQUE INDEX ux_user_settings_user_key
        ON user_settings(user_id, key);
    END IF;
END$$;

-- 5) (Optional) make a primary key via unique index if desired
-- Postgres doesn't require PK for upsert; the unique index is enough for ON CONFLICT.
"""

if __name__ == "__main__":
    print("[migrate user_settings] starting…")
    with session_scope() as s:
        s.execute(text(SQL))
        # quick check
        chk = s.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='user_settings' AND column_name IN ('user_id','key','value','updated_at')
            ORDER BY column_name
        """)).all()
        cols = [c[0] for c in chk]
        print(f"[migrate user_settings] columns={cols}")
    print("[migrate user_settings] ok ✅")
