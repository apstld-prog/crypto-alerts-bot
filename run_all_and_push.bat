@echo off
REM ============================================
REM Full automation: migration + commit + push
REM ============================================

echo [1/4] Activating venv...
call .venv\Scripts\activate

echo [2/4] Running migration script (if exists)...
if exist migrate_add_last_triggered_at.py (
    python migrate_add_last_triggered_at.py
) else (
    echo No migration script found, skipping...
)

echo [3/4] Git commit & push...
git add -A
git commit -m "Auto: migration + latest changes" || echo Nothing to commit.
git push

echo [4/4] Done! Render will auto-redeploy if connected to this repo.
echo.
pause
