#!/usr/bin/env bash
set -Eeuo pipefail

# Move to repo root (where this script lives)
cd "$(dirname "$0")"

# Diagnostics
python -V
pip -V

# Run the single-process server (bot + alerts + health + alerts loop)
exec python server_combined.py
