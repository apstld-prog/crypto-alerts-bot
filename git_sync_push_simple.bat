@echo off
REM =========================================
REM Git Sync & Push (Simple & Compatible)
REM Place this file inside your repo folder and double-click.
REM It will: pull -> add -> commit -> push, and it will pause on errors.
REM =========================================

cd /d %~dp0

echo Checking if this is a Git repository...
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo [ERROR] This folder is not a Git repository.
  echo Put this .bat inside the folder that contains the .git folder and run again.
  pause
  exit /b 1
)

echo.
echo === git pull --rebase ===
git pull --rebase
if errorlevel 1 (
  echo.
  echo [WARNING] Pull/rebase failed. Resolve any conflicts and run again.
  pause
  exit /b 1
)

echo.
echo === git status ===
git status

REM Detect changes
set CHANGES=
for /f "delims=" %%A in ('git status --porcelain') do set CHANGES=1

if not defined CHANGES (
  echo.
  echo No local file changes detected.
  echo If you already committed locally, a push will still happen; otherwise nothing to do.
  echo.
  echo === git push ===
  git push
  echo.
  echo Done.
  pause
  exit /b 0
)

echo.
set /p msg=Enter commit message (default: Update): 
if "%msg%"=="" set msg=Update

echo.
echo === git add --all ===
git add --all

echo.
echo === git commit -m "%msg%" ===
git commit -m "%msg%"
REM Continue even if commit returns non-zero (e.g., nothing to commit)

echo.
echo === git push ===
git push

echo.
echo Done.
pause
