@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo [1/3] Adding ALL changes...
git add --all

echo.
echo [2/3] Commit changes...
set MSG=Auto update
git commit -m "%MSG%"
if errorlevel 1 (
  echo [INFO] Nothing new to commit. Continuing to push...
)

echo.
echo [3/3] Pushing to origin main...
git push -u origin main
if errorlevel 1 (
  echo [WARN] Push failed. Check your connection or credentials.
)

echo.
echo Done. Press any key to close.
pause >nul
