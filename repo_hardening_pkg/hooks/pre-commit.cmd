@echo off
REM pre-commit.cmd — invokes pre-commit.ps1 if PowerShell is available
setlocal

set PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe
if exist "%PS%" (
    "%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0pre-commit.ps1"
    exit /b %errorlevel%
)

REM Fallback: do nothing but allow the commit if PowerShell missing
echo Warning: PowerShell not found. Skipping pre-commit checks.
exit /b 0
