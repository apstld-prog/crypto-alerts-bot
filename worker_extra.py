
import os, time, threading
from typing import List, Tuple
import requests
from sqlalchemy import text
from models_extras import SessionLocalExtra
from features_market import last_5m_change

BOT_TOKEN = os.getenv("BOT_TOKEN")
SYMBOLS_SCAN = [s.strip().upper() for s in (os.getenv("SYMBOLS_SCAN") or "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,PEPEUSDT,SHIBUSDT").split(",") if s.strip()]
DEFAULT_THR = int(os.getenv("PUMP_THRESHOLD_PERCENT", "10"))

_stop = False
_thread = None

def _send(chat_id: str, text_msg: str):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text_msg}, timeout=10)
    except Exception:
        pass

def _tick_once():
    # find who opted in
    with SessionLocalExtra() as s:
        rows = s.execute(text("SELECT user_id, COALESCE(pump_threshold_percent, :d) AS thr FROM user_settings WHERE pump_live = TRUE"),
                         {"d": DEFAULT_THR}).all()
    if not rows:
        return
    # compute changes for symbols
    changes = []
    for sym in SYMBOLS_SCAN:
        try:
            pc = last_5m_change(sym)
        except Exception:
            pc = None
        if pc is not None:
            changes.append((sym, pc))
    if not changes:
        return
    for uid, thr in rows:
        for sym, pc in changes:
            if abs(pc) >= thr:
                arrow = "🚀" if pc > 0 else "🔻"
                _send(str(uid), f"{arrow} Pump alert: {sym} {pc:+.2f}% (≈5m)")

def _loop():
    global _stop
    while not _stop:
        try:
            _tick_once()
        except Exception:
            pass
        time.sleep(30)

def start_pump_watcher():
    global _thread, _stop
    if _thread and _thread.is_alive():
        return
    _stop = False
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()

def stop_pump_watcher():
    global _stop, _thread
    _stop = True
    _thread = None
