# migrate_add_last_triggered_at.py
import os
import sys
from datetime import datetime
from sqlalchemy import create_engine, text

def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set in environment.")
        sys.exit(1)

    engine = create_engine(db_url, pool_pre_ping=True, future=True)
    with engine.begin() as conn:
        # 1) Add column if missing
        conn.execute(text("""
            ALTER TABLE IF NOT EXISTS alerts
            ADD COLUMN IF NOT EXISTS last_triggered_at TIMESTAMPTZ NULL;
        """))

        # 2) Optional: backfill from alert_triggers (if table exists)
        conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name='alert_triggers'
                ) THEN
                    WITH latest AS (
                        SELECT alert_id, MAX(triggered_at) AS ts
                        FROM alert_triggers
                        GROUP BY alert_id
                    )
                    UPDATE alerts a
                    SET last_triggered_at = l.ts
                    FROM latest l
                    WHERE a.id = l.alert_id
                      AND (a.last_triggered_at IS NULL OR a.last_triggered_at < l.ts);
                END IF;
            END$$;
        """))

        # 3) Helpful index
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_alerts_last_triggered_at
            ON alerts (last_triggered_at DESC);
        """))

    print("OK: alerts.last_triggered_at ensured + backfilled (if possible).")

if __name__ == "__main__":
    main()
