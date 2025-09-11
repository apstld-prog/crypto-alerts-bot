# worker.py
import os
import time
import logging
from datetime import datetime
from sqlalchemy import text

from db import init_db, session_scope, engine
from worker_logic import run_alert_cycle  # πρέπει να υπάρχει ήδη στο project σου

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")

# Περιβάλλον
INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))  # μπορείς να το αφήσεις 60"
LOCK_ID = int(os.getenv("ALERTS_LOCK_ID", "911002"))               # advisory lock για να μην τρέχουν 2 workers
ONCE = os.getenv("RUN_ONCE", "0") == "1"                            # για δοκιμή/debug

def try_advisory_lock(lock_id: int) -> bool:
    """Προσπαθεί να πάρει Postgres advisory lock.
    Επιστρέφει True αν το πήρε, False αν το έχει άλλος worker.
    """
    try:
        with engine.connect() as conn:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}).scalar()
            return bool(got)
    except Exception as e:
        log.error("advisory_lock_error: %s", e)
        return False

def main():
    init_db()

    if not try_advisory_lock(LOCK_ID):
        log.warning("Another worker holds the advisory lock (id=%s). Exiting.", LOCK_ID)
        return

    log.info("Alert worker started. interval=%ss lock_id=%s", INTERVAL_SECONDS, LOCK_ID)

    while True:
        ts = datetime.utcnow().isoformat()
        try:
            with session_scope() as session:
                counters = run_alert_cycle(session)  # your business logic
            log.info("alert_cycle ts=%s counters=%s", ts, counters)
        except Exception as e:
            log.exception("alert_cycle_error ts=%s err=%s", ts, e)
        finally:
            # ⚠️ Κρίσιμη γραμμή για χαμηλή κατανάλωση Neon:
            # Κλείνουμε ΟΛΕΣ τις συνδέσεις του pool μετά από κάθε κύκλο,
            # ώστε η βάση να μπορεί να autosuspend μεταξύ των κύκλων.
            try:
                engine.dispose()
            except Exception as e:
                log.warning("engine.dispose() warning: %s", e)

        if ONCE:
            break

        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
