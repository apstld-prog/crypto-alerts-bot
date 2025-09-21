param(
    [Parameter(Mandatory=$true)][string]$RepoUrl,
    [Parameter(Mandatory=$true)][string]$OldTokenPart,
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

# 1) Ensure git-filter-repo is installed
try {
    python -m pip show git-filter-repo | Out-Null
} catch {
    Write-Host "==> Installing git-filter-repo via pip"
    python -m pip install --upgrade git-filter-repo
}

# 2) Prepare workspace
$work = Join-Path $PWD ("cleanup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $work | Out-Null
Set-Location $work

Write-Host "==> Mirror-cloning $RepoUrl"
git clone --mirror $RepoUrl repo.git
Set-Location (Join-Path $work "repo.git")

# 3) Build replace-text file
$repl = @"
$OldTokenPart==>REDACTED
"@
$replPath = Join-Path $PWD "replacements.txt"
$repl | Out-File -Encoding UTF8 $replPath

# 4) Run git-filter-repo replace-text (shell process substitution alternative)
# Create a temp file that git-filter-repo expects
git filter-repo --replace-text $replPath

# 5) Cleanup & push
git push --force --tags

Write-Host ""
Write-Host "DONE. IMPORTANT:" -ForegroundColor Yellow
Write-Host " - Update deployments with NEW token (ENV VAR)."
Write-Host " - Ask collaborators to fresh-clone the repo."
