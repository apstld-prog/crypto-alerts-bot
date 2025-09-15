# Crypto Alerts – Single Service on Render

This setup runs EVERYTHING in a single Render **Web Service**:
- Telegram bot (polling)
- Alerts loop (background thread)
- FastAPI health server (for port binding)

No blueprint. No worker. One service only.

## Files
- `server_combined.py` — single-process app (bot + alerts + health + alerts loop)
- `start.sh` — tiny runner used by Render Start Command
- `requirements.txt` — dependencies
- Extra features pack:
  - `features_market.py`, `commands_extra.py`, `worker_extra.py`, `models_extras.py`, `migrate_user_settings.py`

## Render → New Web Service
1. Connect your repo.
2. **Environment**: Python 3.11 (or higher).
3. **Build Command**:  
