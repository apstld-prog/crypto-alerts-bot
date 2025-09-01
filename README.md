# Crypto Alerts — Deploy & Usage Guide
_Last updated: 2025-09-01 21:02_

This guide shows how to deploy your Crypto Alerts bot using **Render** (Web + Worker) and an external **Postgres DB** (Neon/Supabase).

---

## 1) Prepare Postgres DB
### Option A — Neon
- Sign up at https://neon.tech
- Create **New Project** → pick **EU region**.
- Copy connection string:
  ```
  postgresql://<USER>:<PASS>@<HOST>.neon.tech/<DBNAME>?sslmode=require
  ```
- This becomes your `DATABASE_URL`.

### Option B — Supabase
- Sign up at https://supabase.com
- New Project → Database Settings → copy connection string:
  ```
  postgresql://postgres:<PASS>@<HOST>.supabase.co:5432/postgres?sslmode=require
  ```

---

## 2) Deploy on Render
- Create two services:
  1. **Web Service** → runs `server_combined.py`
  2. **Background Worker** → runs `worker.py`

Or use the included **render.yaml** blueprint.

### Web Service Settings
- Build Command:
  ```bash
  pip install --upgrade pip
  pip install -r requirements.txt
  ```
- Start Command:
  ```bash
  gunicorn server_combined:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60
  ```
- Health Check Path: `/healthz`

### Worker Settings
- Build Command: same as Web
- Start Command:
  ```bash
  python worker.py
  ```

---

## 3) Environment Variables
Set these in both **Web** and **Worker**:

```
ENV=production
PYTHON_VERSION=3.11.9
DATABASE_URL=postgresql://<USER>:<PASS>@<HOST>/<DBNAME>?sslmode=require
ALERTS_SECRET=<your-long-random-secret>
BOT_TOKEN=<your-telegram-bot-token>
TELEGRAM_CHAT_ID=<optional-default-chat-id>
```

Additional:
- Web only: `ADMIN_KEY=<admin-secret>`
- Worker only: `WORKER_INTERVAL_SECONDS=60`
- Optional (Web): `STRIPE_SECRET`, `STRIPE_WEBHOOK_SECRET`

---

## 4) Cron Job
On Render → **Cron Jobs** → create job:
```
curl -sS "https://<WEB-URL>/cron?key=$ALERTS_SECRET" -m 25
```
Run every 2 minutes.

---

## 5) Health Check
```bash
curl -s https://<WEB-URL>/healthz
```
Expect: {"ok": true}

---

## 6) Stats
```bash
curl -s https://<WEB-URL>/stats | jq
```
Shows users, premium, alerts, subscriptions.

---

## 7) Admin Endpoints
Require `ADMIN_KEY`.

```bash
curl -s "https://<WEB-URL>/admin/users?key=<ADMIN_KEY>" | jq
curl -s "https://<WEB-URL>/admin/alerts?key=<ADMIN_KEY>" | jq
curl -s "https://<WEB-URL>/admin/subscriptions?key=<ADMIN_KEY>" | jq
```

---

## 8) Telegram Bot
- Verify bot:
  ```bash
  curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMe"
  ```
- Interact in Telegram: `/start`, `/help`, `/stats`

---

## 9) Demo Data (optional)
To insert demo user, alert, subscription:
```bash
psql "$DATABASE_URL" -f seed.sql
```

---

## 10) Troubleshooting
- **500 on /healthz** → Check `DATABASE_URL` and add `?sslmode=require`.
- **403 on /cron** → Wrong or missing `ALERTS_SECRET`.
- **Worker not looping** → Check logs; ensure `WORKER_INTERVAL_SECONDS` set.
- **Telegram not sending** → Wrong `BOT_TOKEN` or missing `TELEGRAM_CHAT_ID`.
- **Data resets after redeploy** → You’re using SQLite, not external DB.
