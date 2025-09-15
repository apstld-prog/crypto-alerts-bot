#!/usr/bin/env bash
set -euo pipefail

# Safety: make sure we are in repo root
cd "$(dirname "$0")"

# Show versions (handy in Render logs)
python -V
pip -V

# Run the single-process server:
# - starts FastAPI health server (binds $PORT)
# - runs Telegram bot (polling)
# - runs alerts loop
exec python server_combined.py
