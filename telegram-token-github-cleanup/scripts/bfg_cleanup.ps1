param(
    [Parameter(Mandatory=$true)][string]$RepoUrl,
    [Parameter(Mandatory=$true)][string]$OldTokenPart,
    [string]$Branch = "main",
    [string]$BfgVersion = "1.14.0"
)

$ErrorActionPreference = "Stop"

# 1) Prepare workspace
$work = Join-Path $PWD ("cleanup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $work | Out-Null
Set-Location $work

Write-Host "==> Mirror-cloning $RepoUrl"
git clone --mirror $RepoUrl repo.git

# 2) Download BFG jar (if not present)
$jar = Join-Path $work ("bfg-" + $BfgVersion + ".jar")
if (!(Test-Path $jar)) {
    $url = "https://repo1.maven.org/maven2/com/madgag/bfg-repo-cleaner/$BfgVersion/bfg-$BfgVersion.jar"
    Write-Host "==> Downloading BFG $BfgVersion from $url"
    Invoke-WebRequest -Uri $url -OutFile $jar
}

# 3) Build replacements.txt (regex to catch Telegram-like tokens + specific old part)
$repl = @"
regex:(\b\d{6}:[A-Za-z0-9_-]{20,}\b)==>REDACTED
$OldTokenPart==>REDACTED
"@
$replPath = Join-Path $work "replacements.txt"
$repl | Out-File -Encoding UTF8 $replPath

# 4) Run BFG
Set-Location (Join-Path $work "repo.git")
Write-Host "==> Running BFG replace-text"
java -jar $jar --replace-text $replPath .

# 5) Cleanup & push
Write-Host "==> Expiring reflog & garbage-collecting"
git reflog expire --expire=now --all
git gc --prune=now --aggressive

Write-Host "==> Force pushing cleaned history and tags"
git push --force --tags

Write-Host ""
Write-Host "DONE. IMPORTANT:" -ForegroundColor Yellow
Write-Host " - Update your deployments with the NEW token (ENV VAR)."
Write-Host " - Ask collaborators to fresh-clone the repo."
Write-Host " - Verify with 'git grep' that the old token part is gone."
