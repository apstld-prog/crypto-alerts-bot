# Crypto Alerts — Deploy & Usage Guide

This guide shows how to deploy your Crypto Alerts bot using **Render** (Web + Worker) and an external **Postgres DB** (Neon/Supabase).

## 1) Prepare Postgres DB
- Neon: create project (EU), copy connection string and use as `DATABASE_URL` (add `?sslmode=require` if needed).
- Supabase: new project, Database → connection string → use as `DATABASE_URL` (with `?sslmode=require`).

## 2) Deploy on Render
- Web Service: build
  - `pip install --upgrade pip`
  - `pip install -r requirements.txt`
- Start: `gunicorn server_combined:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60`
- Health Check: `/healthz`
- Worker: build same as web, start `python worker.py`

## 3) Environment Variables (Web & Worker)
```
ENV=production
PYTHON_VERSION=3.11.9
DATABASE_URL=postgresql://<USER>:<PASS>@<HOST>/<DBNAME>?sslmode=require
ALERTS_SECRET=<your-long-random-secret>
BOT_TOKEN=<your-telegram-bot-token>
TELEGRAM_CHAT_ID=<optional-default-chat-id>
```
- Web only: `ADMIN_KEY=<admin-secret>`
- Worker only: `WORKER_INTERVAL_SECONDS=60`

## 4) Health
```
curl -s https://<WEB-URL>/healthz
curl -s https://<WEB-URL>/stats | jq
```

## 5) Admin
```
curl -s "https://<WEB-URL>/admin/users?key=<ADMIN_KEY>" | jq
curl -s "https://<WEB-URL>/admin/alerts?key=<ADMIN_KEY>" | jq
curl -s "https://<WEB-URL>/admin/subscriptions?key=<ADMIN_KEY>" | jq
```

## 6) Seed demo data
```
psql "$DATABASE_URL" -f seed.sql
```
