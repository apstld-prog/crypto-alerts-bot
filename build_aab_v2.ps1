# build_aab_v2.ps1
param(
  [string]$AppDir = "cryptoalerts77",
  [switch]$Clean = $true
)

$ErrorActionPreference = "Stop"

function Require-Cmd($name) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "Required command not found: $name"
  }
}

Require-Cmd "flutter"

if (-not (Test-Path $AppDir)) {
  throw "App directory not found: $AppDir (run setup script first)"
}

Push-Location $AppDir
try {
  if ($Clean) { flutter clean | Out-Host }
  flutter pub get | Out-Host
  flutter build appbundle --release
  $aab = Join-Path (Get-Location) 'build\app\outputs\bundle\release\app-release.aab'
  Write-Host "✅ AAB at: $aab"
  Write-Host ""
  Write-Host "ℹ️ If signing fails:"
  Write-Host "   - Create upload keystore (keytool) and configure android/app/build.gradle signingConfigs release"
  Write-Host "   - Or enable Play App Signing and use an Upload key locally"
} finally {
  Pop-Location
}
