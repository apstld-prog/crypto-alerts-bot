# worker.py
import os
import time
import logging
from datetime import datetime
from sqlalchemy import text

from db import init_db, session_scope, engine, masked_db_url
from worker_logic import run_alert_cycle

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")

INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))  # 60s = near real-time
LOCK_ID = int(os.getenv("ALERTS_LOCK_ID", "911002"))
RUN_ONCE = os.getenv("RUN_ONCE", "0") == "1"

def try_advisory_lock(lock_id: int) -> bool:
    """Postgres advisory lock: επιτρέπει μόνο έναν worker να τρέχει το loop."""
    try:
        with engine.connect() as conn:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}).scalar()
            return bool(got)
    except Exception as e:
        log.error("advisory_lock_error: %s", e)
        return False

def main():
    init_db()
    log.info("DB=%s", masked_db_url())

    if not try_advisory_lock(LOCK_ID):
        log.warning("Another worker already holds the lock id=%s. Exiting.", LOCK_ID)
        return

    log.info("Alert worker started. interval=%ss lock_id=%s", INTERVAL_SECONDS, LOCK_ID)

    while True:
        ts = datetime.utcnow().isoformat()
        try:
            with session_scope() as session:
                counters = run_alert_cycle(session)
            log.info("alert_cycle ts=%s counters=%s", ts, counters)
        except Exception as e:
            log.exception("alert_cycle_error ts=%s err=%s", ts, e)
        finally:
            # Σημαντικό για Neon: κλείνουμε το pool μετά από κάθε κύκλο
            try:
                engine.dispose()
            except Exception as e:
                log.warning("engine.dispose() warning: %s", e)

        if RUN_ONCE:
            break

        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
