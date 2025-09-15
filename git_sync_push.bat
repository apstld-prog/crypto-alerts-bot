@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

REM =========================================
REM Git Sync & Push (easy + fast)
REM Place this file inside your repo folder and double‑click it.
REM It will: pull -> add -> commit -> push (with basic conflict handling).
REM =========================================

REM Move to the folder where this script lives (repo root ideally)
cd /d %~dp0

REM Safety: Ensure we're in a Git repo
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo [ERROR] This folder is not a Git repository.
  echo Place git_sync_push.bat inside your repo (where the .git folder is) and run again.
  pause
  exit /b 1
)

echo.
echo === Pulling latest changes (rebase) ===
git pull --rebase
if errorlevel 1 (
  echo.
  echo [WARNING] Rebase/pull could not complete automatically.
  echo Resolve the conflicts shown by Git (open the files, fix markers <<<<<<< ======= >>>>>>>),
  echo then run this script again.
  pause
  exit /b 1
)

REM Check if there are any local changes to commit
set CHANGES=
for /f "delims=" %%A in ('git status --porcelain') do set CHANGES=1

if not defined CHANGES (
  echo.
  echo No local changes detected. Pushing any pending commits (if any)...
  git push
  echo.
  echo Done!
  pause
  exit /b 0
)

echo.
set /p msg=Enter commit message (leave blank for "Update"): 
if "%msg%"=="" set msg=Update

echo.
echo === Adding changes ===
git add --all

echo.
echo === Committing ===
git commit -m "%msg%"
REM If nothing to commit (e.g., only whitespace), continue gracefully
REM (Git returns non-zero when there's nothing new, but we still want to push)
REM We won't stop on commit error.

echo.
echo === Pushing to origin ===
git push

echo.
echo Done!
pause
