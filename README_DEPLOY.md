# Render Deploy Pack (Frankfurt) — 2025-08-31 22:06

Files:
- render.yaml — Web + Worker, region frankfurt, Python 3.11.9
- .python-version — locks Python to 3.11.9 from repo
- .env.example — environment keys to set on Render
- Makefile — helper commands

Steps:
1) Put these files at the repo root. Commit & push.
2) Create Managed Postgres in Frankfurt. Copy the External Connection String.
3) Render → Blueprints → New from repo → select repo → Apply.
4) Open both services (web & worker) → Environment → add keys from .env.example (same values).
5) Manual Deploy → Clear build cache once → Deploy.
6) Check /healthz on web, and worker logs.
7) Add a Cron Job to call: curl -sS "https://<WEB-URL>/cron?key=$ALERTS_SECRET" -m 25
