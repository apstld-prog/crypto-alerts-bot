# pre-commit.ps1 — Windows PowerShell git hook
# Place as .git/hooks/pre-commit.ps1 and (optionally) use pre-commit.cmd to invoke it.

$ErrorActionPreference = "Stop"

# Staged diff only
$diff = git diff --cached

# Telegram token pattern
$telegramPattern = '\b[0-9]{6,}:[A-Za-z0-9_-]{20,}\b'
if ($diff -match $telegramPattern) {
    Write-Host "❌ Detected a string that looks like a Telegram Bot Token in staged changes." -ForegroundColor Red
    Write-Host "   Remove it and use environment variables (.env) instead."
    exit 1
}

# Prevent committing .env files
$stagedFiles = git diff --cached --name-only
if ($stagedFiles -match '(^|/)\.env($|\.|/)') {
    Write-Host "❌ Attempt to commit a .env file detected. This file must not be committed." -ForegroundColor Red
    exit 1
}

# Heuristic for secrets
if ($diff -match '(?i)(api[_-]?key|secret|token|password)\s*=\s*[''\"].+[''\" ]') {
    Write-Host "⚠  Possible secret-looking assignment found. Double-check before committing." -ForegroundColor Yellow
    $ans = Read-Host "Continue anyway? [y/N]"
    if ($ans -notmatch '^[yY]$') { exit 1 }
}

exit 0
