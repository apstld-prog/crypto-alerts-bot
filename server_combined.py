#!/usr/bin/env python3
import os, threading, time
from payments_webhook import app as flask_app
from bot import run_bot
from waitress import serve  # production WSGI server

def run_flask():
    port = int(os.getenv("PORT", "8000"))
    serve(flask_app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    time.sleep(1)
    run_bot()
