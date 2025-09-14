# migrate_user_seq.py
from sqlalchemy import text
from db import session_scope

SQL = """
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS user_seq INTEGER;

WITH ranked AS (
  SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) AS rn
  FROM alerts
)
UPDATE alerts a
SET user_seq = r.rn
FROM ranked r
WHERE a.id = r.id
  AND (a.user_seq IS NULL OR a.user_seq <> r.rn);

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

if __name__ == "__main__":
    print("[migrate user_seq] starting…")
    with session_scope() as s:
        s.execute(text(SQL))
        row = s.execute(text("SELECT COUNT(*) FROM alerts")).first()
        print(f"[migrate user_seq] done. alerts_total={row[0]}")
    print("[migrate user_seq] ok ✅")
