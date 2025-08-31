# Crypto Alerts – Starter Pack
Updated: 2025-08-31 20:29

This pack includes a minimal **Web API (FastAPI)**, a **Background Worker**, and a **Telegram Bot** wired to a **Postgres** DB.
It is designed to run on **Render** with separate Web and Worker services, plus a cron hitting `/cron`.

## Files
- `server_combined.py` – FastAPI app with `/`, `/healthz`, `/cron`, `/stats`, and `/webhooks/stripe`.
- `worker.py` – Long-running worker loop (for Render Background Worker) calling `run_alert_cycle()` every N seconds.
- `worker_logic.py` – Core alert logic (fetch prices, evaluate rules, notify Telegram).
- `db.py` – SQLAlchemy models & DB session helpers.
- `bot.py` – Minimal Telegram bot (python-telegram-bot v20) with `/start` and `/stats`.
- `requirements.txt` – Python dependencies.
- `.env.example` – Environment variables you must set (copy to `.env` locally).

## Quickstart (Local)
1. Python 3.10+ recommended.
2. `pip install -r requirements.txt`
3. Copy `.env.example` → `.env` and fill values.
4. Initialize DB tables (auto-created on first run by `db.py`). Optional: run `python server_combined.py` once.
5. Start Web: `uvicorn server_combined:app --host 0.0.0.0 --port 8000`
6. Start Worker: `python worker.py`
7. Start Bot (optional): `python bot.py`

Open http://127.0.0.1:8000/ (or `/docs`).

## Render Setup (Paid)
- Create Managed Postgres → put connection string to `DATABASE_URL` in both Web and Worker services.
- Web service Start Command (Render):
  ```bash
  gunicorn server_combined:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60
  ```
- Worker service Start Command (Render):
  ```bash
  python worker.py
  ```
- Cron Job (every 2 minutes):
  ```bash
  curl -sS "https://<your-domain>/cron?key=$ALERTS_SECRET" -m 25
  ```

## Stripe Webhook (optional)
- Set endpoint to `https://<your-domain>/webhooks/stripe`
- Put `STRIPE_SECRET`, `STRIPE_WEBHOOK_SECRET` in env.
- Use dashboard to send a test event and check logs.

## Notes
- This starter is intentionally simple: no migrations (tables auto-create), minimal error handling.
- Replace the naive price fetch with your preferred provider(s).
