# migrate_user_seq.py
# Adds alerts.user_seq and backfills per-user sequence. Idempotent.
from sqlalchemy import text
from db import session_scope

SQL = """
-- 1) Add column if missing
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS user_seq INTEGER;

-- 2) Backfill per-user sequence based on creation order (id)
WITH ranked AS (
  SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) AS rn
  FROM alerts
)
UPDATE alerts a
SET user_seq = r.rn
FROM ranked r
WHERE a.id = r.id
  AND (a.user_seq IS NULL OR a.user_seq <> r.rn);

-- 3) Create unique index (user_id, user_seq) if missing
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND indexname = 'uniq_alerts_user_seq'
  ) THEN
    CREATE UNIQUE INDEX uniq_alerts_user_seq ON alerts (user_id, user_seq);
  END IF;
END$$;
"""

CHECKS = """
SELECT
  COUNT(*) AS alerts_total,
  COUNT(user_seq) AS alerts_with_seq
FROM alerts;
"""

if __name__ == "__main__":
    print("[migrate user_seq] starting…")
    with session_scope() as s:
        s.execute(text(SQL))
        row = s.execute(text(CHECKS)).first()
        print(f"[migrate user_seq] done. alerts_total={row.alerts_total}, alerts_with_seq={row.alerts_with_seq}")
    print("[migrate user_seq] ok ✅")
