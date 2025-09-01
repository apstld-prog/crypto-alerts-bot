# Crypto Alerts — Deploy & Operations Guide

This pack contains everything you need to deploy on **Render (free)** with an **external Postgres** (Neon/Supabase).

## Steps
1) Create Neon (or Supabase) Postgres → copy the connection string → set as `DATABASE_URL`.
2) Deploy Web & Worker on Render (Blueprint `render.yaml` or manual).
3) Set env vars on both services (see `.env.example`).
4) (Optional) Load demo data:
   ```bash
   psql "$DATABASE_URL" -f seed.sql
   ```
5) Health check:
   ```bash
   curl -s https://<WEB-URL>/healthz
   curl -s https://<WEB-URL>/stats | jq
   ```
6) Admin (needs `ADMIN_KEY`):
   ```bash
   curl -s "https://<WEB-URL>/admin/users?key=<ADMIN_KEY>" | jq
   ```

## Files
- `server_combined.py`, `worker.py`, `worker_logic.py`, `db.py`, `bot.py`
- `requirements.txt`, `.env.example`, `render.yaml`, `seed.sql`

## Notes
- Python pinned at **3.11.9** (avoid SQLAlchemy issues on 3.13).
- DB tables auto-create on startup.
