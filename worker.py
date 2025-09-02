
import os
import time
from datetime import datetime

from db import init_db, session_scope
from worker_logic import run_alert_cycle

INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))

def main():
    print({"msg": "worker_start", "interval": INTERVAL_SECONDS})
    init_db()
    while True:
        ts = datetime.utcnow().isoformat()
        try:
            with session_scope() as session:
                counters = run_alert_cycle(session)
            print({"msg": "alert_cycle", "ts": ts, **counters})
        except Exception as e:
            print({"msg": "worker_error", "ts": ts, "error": str(e)})
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
