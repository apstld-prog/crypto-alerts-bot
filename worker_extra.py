# worker_extra.py
# Background helpers for optional features.
# - start_pump_watcher(): keeps name for backward-compatibility AND starts the daily news scheduler
# - Daily News Scheduler: sends a digest every day at 09:00 UTC to opted-in users
#
# Single-service friendly: runs lightweight loops in daemon threads.

import os
import time
import threading
from datetime import datetime, timezone, date

import requests
from sqlalchemy import text

from db import session_scope, User
from models_extras import get_user_setting, set_user_setting
from plans import build_plan_info
from features_market import get_news_headlines

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DAILYNEWS_HOUR_UTC = int(os.getenv("DAILYNEWS_HOUR_UTC", "9"))
DAILYNEWS_MAX_FREE = 1
DAILYNEWS_MAX_PREMIUM = min(30, int(os.getenv("DAILYNEWS_MAX_PREMIUM", "10")))

_ADMIN_IDS = {s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()}

def _send_message(chat_id: str, text: str, disable_preview: bool = False) -> bool:
    if not BOT_TOKEN:
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview
        }, timeout=15)
        return r.status_code == 200
    except Exception:
        return False

def _list_dailynews_optins():
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT u.id, u.telegram_id
            FROM users u
            JOIN user_settings us ON us.user_id = u.id
            WHERE us.key = 'dailynews' AND us.value = 'on'
        """)).all()
        return [(str(r.telegram_id), int(r.id)) for r in rows if r.telegram_id]

def _should_send_today(user_id: int) -> bool:
    last = get_user_setting(str(user_id), "dailynews_last") or ""
    today = date.today().isoformat()
    return last != today

def _mark_sent_today(user_id: int):
    set_user_setting(str(user_id), "dailynews_last", date.today().isoformat())

def _build_digest_for(telegram_id: str, user_id: int) -> str:
    # Check plan
    plan = build_plan_info(telegram_id, _ADMIN_IDS)
    if plan.has_unlimited:
        limit = DAILYNEWS_MAX_PREMIUM
    else:
        limit = DAILYNEWS_MAX_FREE

    items = get_news_headlines(limit=limit)
    if not items:
        return ""
    title = "🗞️ <b>Daily Crypto Digest</b>\n"
    lines = [title]
    for t, link in items:
        safe_title = t.replace("\n", " ").strip()
        lines.append(f"• <a href=\"{link}\">{safe_title}</a>")
    return "\n".join(lines)

def _daily_news_loop():
    print({"msg": "daily_news_scheduler_started", "hour_utc": DAILYNEWS_HOUR_UTC})
    last_run_day = None  # extra guard to avoid repeating inside same day if process keeps running

    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour == DAILYNEWS_HOUR_UTC:
                today_str = now.date().isoformat()
                if last_run_day != today_str:
                    # time window 09:00 UTC (single shot per day)
                    optins = _list_dailynews_optins()
                    if optins:
                        for tg_id, uid in optins:
                            if not _should_send_today(uid):
                                continue
                            msg = _build_digest_for(tg_id, uid)
                            if not msg:
                                continue
                            ok = _send_message(tg_id, msg, disable_preview=False)
                            if ok:
                                _mark_sent_today(uid)
                    last_run_day = today_str
            time.sleep(30)  # check twice per minute
        except Exception as e:
            print({"msg": "daily_news_scheduler_error", "error": str(e)})
            time.sleep(30)

# ---- Pump watcher (minimal placeholder for compatibility) ----
def _pump_watcher_loop():
    # Minimal safe loop — extend with real-time pump logic if needed.
    print({"msg": "pump_watcher_started"})
    while True:
        try:
            # Placeholder sleep to keep thread alive
            time.sleep(15)
        except Exception as e:
            print({"msg": "pump_watcher_error", "error": str(e)})
            time.sleep(15)

def start_pump_watcher():
    """
    Backward-compatible entry point.
    Starts both:
      - pump watcher loop (lightweight placeholder)
      - daily news scheduler loop (09:00 UTC)
    """
    t1 = threading.Thread(target=_pump_watcher_loop, daemon=True)
    t1.start()

    t2 = threading.Thread(target=_daily_news_loop, daemon=True)
    t2.start()

    print({"msg": "worker_extra_threads_started"})
