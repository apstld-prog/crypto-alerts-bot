@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

REM =========================================
REM Git Sync & Push (VERBOSE)
REM Shows diagnostics and writes a log file in the same folder.
REM =========================================

cd /d %~dp0

set LOGFILE=%~dp0git_sync_push.log
echo [%%DATE%% %%TIME%%] ===== Run started in: %CD% > "%LOGFILE%"
echo.

echo Repo check...
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo [ERROR] This folder is not a Git repository.
  echo [ERROR] This folder is not a Git repository. >> "%LOGFILE%"
  echo Place this .bat inside your repo (where the .git folder is) and run again.
  pause
  exit /b 1
)

echo Current branch / remotes:
git branch >> "%LOGFILE%"
git remote -v >> "%LOGFILE%"

echo.
echo === git pull --rebase ===
echo === git pull --rebase === >> "%LOGFILE%"
git pull --rebase | tee -a "%LOGFILE%"
if errorlevel 1 (
  echo.
  echo [WARNING] Pull/rebase failed. Check the log file: %LOGFILE%
  echo Resolve conflicts (<<<<<<< ======= >>>>>>>) and run again.
  pause
  exit /b 1
)

echo.
echo === git status (before) ===
git status | tee -a "%LOGFILE%"

REM Detect changes
set CHANGES=
for /f "delims=" %%A in ('git status --porcelain') do set CHANGES=1

if not defined CHANGES (
  echo.
  echo No local changes to commit.
  echo Attempting plain push in case there are pending local commits...
  echo No changes; plain push >> "%LOGFILE%"
  git push | tee -a "%LOGFILE%"
  echo.
  echo Done. See log: %LOGFILE%
  pause
  exit /b 0
)

echo.
set /p msg=Enter commit message (leave blank for "Update"): 
if "%msg%"=="" set msg=Update

echo.
echo === git add --all ===
echo === git add --all === >> "%LOGFILE%"
git add --all

echo.
echo === git commit -m "%msg%" ===
echo === git commit -m "%msg%" === >> "%LOGFILE%"
git commit -m "%msg%" | tee -a "%LOGFILE%"
REM Continue even if commit returns non-zero (e.g. nothing to commit)

echo.
echo === git push ===
echo === git push === >> "%LOGFILE%"
git push | tee -a "%LOGFILE%"

echo.
echo Done. See log: %LOGFILE%
pause
