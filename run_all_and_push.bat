@echo off
REM ============================================
REM Full automation: migration + commit + push
REM ============================================

cd /d "%~dp0"

echo [1/5] Activating venv...
if exist ".venv\Scripts\activate" (
  call .venv\Scripts\activate
) else (
  echo WARNING: .venv not found. Continuing without venv...
)

echo [2/5] Running migrations (if present)...
if exist migrate_add_last_triggered_at.py (
  python migrate_add_last_triggered_at.py
) else (
  echo No migration scripts found, skipping...
)

echo [3/5] Git stage & commit...
git add -A
git commit -m "Auto: migration + latest changes" || echo Nothing to commit.

echo [4/5] Pushing to origin/main...
git push -u origin main

echo [5/5] Done. If Render is linked, it will redeploy automatically.
echo.
pause
