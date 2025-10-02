#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
pip install -r requirements.txt

export PYTHONUNBUFFERED=1
uvicorn server_combined:health_app --host 0.0.0.0 --port "${PORT:-8080}"
