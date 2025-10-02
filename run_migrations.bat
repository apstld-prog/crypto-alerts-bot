@echo off
REM ============================================
REM Run database migrations (last_triggered_at etc.)
REM ============================================

echo [1/2] Activating venv...
call .venv\Scripts\activate

echo [2/2] Running migration script...
python migrate_add_last_triggered_at.py

echo.
echo Migration finished. Press any key to exit.
pause >nul
