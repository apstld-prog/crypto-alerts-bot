-- Add per-user sequential number for alerts
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS user_seq INTEGER;

-- Backfill existing rows with per-user sequence based on creation order (id)
WITH ranked AS (
  SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) AS rn
  FROM alerts
)
UPDATE alerts a
SET user_seq = r.rn
FROM ranked r
WHERE a.id = r.id
  AND (a.user_seq IS NULL OR a.user_seq <> r.rn);

-- Ensure uniqueness per user (each user_seq appears once per user)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND indexname = 'uniq_alerts_user_seq'
  ) THEN
    CREATE UNIQUE INDEX uniq_alerts_user_seq
      ON alerts (user_id, user_seq);
  END IF;
END$$;
