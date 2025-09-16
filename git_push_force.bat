$ErrorActionPreference = "Continue"
Set-StrictMode -Version Latest

Write-Host "`n[1/6] Checking repository..."
git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "[ERROR] Not a git repo. Run this inside the folder that contains .git."
  Read-Host "Press Enter to exit"
  exit 1
}

Write-Host "`n[2/6] Pull --rebase..."
git pull --rebase

Write-Host "`n[3/6] Status BEFORE add:"
git status

Write-Host "`n[4/6] Add --all..."
git add --all

$msg = Read-Host "Enter commit message (default: Update)"
if ([string]::IsNullOrWhiteSpace($msg)) { $msg = "Update" }

Write-Host "`n[5/6] Commit..."
git commit -m "$msg"
if ($LASTEXITCODE -ne 0) {
  Write-Host "[INFO] Nothing to commit. Creating empty commit to trigger deploy..."
  git commit --allow-empty -m "$msg"
}

Write-Host "`n[6/6] Push..."
git push -u origin main
if ($LASTEXITCODE -ne 0) {
  Write-Host "[WARN] Push with upstream failed, trying plain push..."
  git push
}

Write-Host "`nDone."
Read-Host "Press Enter to close"
