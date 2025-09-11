# Crypto Alerts – Render Pack

This pack gives you a clean 2-service setup on Render:

- **Web service** (`crypto-alerts-bot`): runs a tiny FastAPI health server (for port binding) **and** the Telegram bot (polling). Alerts are **disabled** here.
- **Worker service** (`crypto-alerts-worker`): runs the alerts loop. No polling here.

## Files
- `worker_logic.py` — includes `resolve_symbol()` and `fetch_price_binance()` to satisfy imports.
- `web_health.py` — minimal FastAPI `GET /` and `GET /health` so the web service binds `$PORT`.
- `render.yaml` — defines the two Render services and start commands.
- `.env.example` — shows exactly what env vars to set per service.

> Your existing files (`daemon.py`, `worker.py`, `db.py`, etc.) are expected to be present. If not, copy them into the repo root.

## Deploy on Render

1. Create a new repo with all project files (including this pack).
2. In Render:
   - **New +** → **Blueprint** → point to your repo.
   - Render reads `render.yaml` and proposes two services.
3. Set **Environment Variables** (use `.env.example` as a guide):
   - For **both** services: `BOT_TOKEN`, `DATABASE_URL`, `ADMIN_TELEGRAM_IDS`, and optional billing vars.
   - The blueprint already sets `RUN_BOT/RUN_ALERTS/WEB_CONCURRENCY/WORKER_INTERVAL_SECONDS` appropriately.
4. Deploy.

## Verify

- Web logs should show: `delete_webhook ... "Webhook is already deleted"` and `bot_start` (no conflicts).
- Worker logs should show periodic `alert_cycle` counters.

## Common pitfalls

- **Conflict: terminated by other getUpdates** → you have more than one poller. Ensure only the web service has `RUN_BOT=1` and `WEB_CONCURRENCY=1`. Do **not** run the bot locally at the same time.
- **No alerts firing** → ensure the worker runs (`RUN_ALERTS=1`), your `users.telegram_id` is numeric, and your alert crosses the threshold (first cycle requires a crossing).

Good luck! 🚀
