# prepare_and_build_signed_v2.ps1
param(
  [string]$AppDir = "cryptoalerts77",
  [string]$PackageName = "com.cryptoalerts77.app",

  # Upload keystore settings (change if you want)
  [string]$KeystorePath = "$HOME\cryptoalerts77-upload-keystore.jks",
  [string]$KeyAlias = "upload",
  [string]$StorePassword = "cryptoalerts77pass",
  [string]$KeyPassword = "cryptoalerts77pass",

  [switch]$SkipKeystore = $false
)

$ErrorActionPreference = "Stop"

function Require-Cmd($name) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "Required command not found: $name"
  }
}

Require-Cmd "flutter"
Require-Cmd "keytool"

Write-Host "Step 1/6: Ensure Flutter project exists -> $AppDir"
if (-not (Test-Path $AppDir)) {
  flutter create $AppDir | Out-Host
}

# Ensure Android platform exists
$androidGradle = Join-Path $AppDir "android\app\build.gradle"
if (-not (Test-Path $androidGradle)) {
  Push-Location $AppDir
  try { flutter create . --platforms=android | Out-Host } finally { Pop-Location }
}

Write-Host "Step 2/6: Overlay code (lib/, pubspec.yaml, Android files)"
Copy-Item -Path ".\lib" -Destination $AppDir -Recurse -Force
Copy-Item -Path ".\pubspec.yaml" -Destination $AppDir -Force

$androidApp = Join-Path $AppDir "android\app"
$androidSrcMain = Join-Path $androidApp "src\main"
$androidRes = Join-Path $androidSrcMain "res"
New-Item -ItemType Directory -Force -Path $androidRes | Out-Null

Copy-Item -Path ".\ANDROID_FILES\src\main\AndroidManifest.xml" -Destination $androidSrcMain -Force
Copy-Item -Path ".\ANDROID_FILES\src\main\res\values" -Destination $androidRes -Recurse -Force
Copy-Item -Path ".\ANDROID_FILES\src\main\res\values-el" -Destination $androidRes -Recurse -Force
$mips = @("mipmap-mdpi","mipmap-hdpi","mipmap-xhdpi","mipmap-xxhdpi","mipmap-xxxhdpi")
foreach ($d in $mips) {
  $src = ".\ANDROID_FILES\src\main\res\$d"
  $dst = Join-Path $androidRes $d
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  if (Test-Path $src) { Copy-Item -Path (Join-Path $src "*") -Destination $dst -Force }
}

# Ensure applicationId
$buildGradle = Join-Path $androidApp "build.gradle"
if (Test-Path $buildGradle) {
  $content = Get-Content $buildGradle -Raw
  if ($content -match 'applicationId\s+"[^"]*"') {
    $content = [regex]::Replace($content, 'applicationId\s+"[^"]*"', ('applicationId "' + $PackageName + '"'))
    $content | Set-Content -Path $buildGradle -Encoding UTF8
  } else {
    Write-Warning "applicationId not found in build.gradle. Set manually to $PackageName"
  }
} else {
  Write-Warning "Missing build.gradle: $buildGradle"
}

Write-Host "Step 3/6: flutter pub get"
Push-Location $AppDir
try {
  flutter pub get | Out-Host
} finally { Pop-Location }

if (-not $SkipKeystore) {
  Write-Host "Step 4/6: Generate upload keystore (if not exists) -> $KeystorePath"
  if (-not (Test-Path $KeystorePath)) {
    & keytool -genkey -v `
      -keystore $KeystorePath `
      -alias $KeyAlias `
      -keyalg RSA `
      -keysize 2048 `
      -validity 10000 `
      -storepass $StorePassword `
      -keypass $KeyPassword `
      -dname "CN=CryptoAlerts77, OU=Org, O=Company, L=City, S=State, C=GR" | Out-Host
  } else {
    Write-Host "Keystore already exists, skipping generation."
  }

  Write-Host "Step 5/6: Create key.properties and patch build.gradle for release signing"
  $keyProps = @"
storePassword=$StorePassword
keyPassword=$KeyPassword
keyAlias=$KeyAlias
storeFile=$KeystorePath
"@
  $androidDir = Join-Path $AppDir "android"
  $keyPropsPath = Join-Path $androidDir "key.properties"
  $keyProps | Set-Content -Path $keyPropsPath -Encoding UTF8

  # Patch android/app/build.gradle
  $gradlePath = Join-Path $AppDir "android\app\build.gradle"
  if (Test-Path $gradlePath) {
    $g = Get-Content $gradlePath -Raw

    if ($g -notmatch 'def keystoreProperties = new Properties\(\)') {
      $injectTop = @"
def keystoreProperties = new Properties()
def keystorePropertiesFile = rootProject.file("key.properties")
if (keystorePropertiesFile.exists()) {
    keystoreProperties.load(new FileInputStream(keystorePropertiesFile))
}
"@
      $g = $g -replace '(?s)android\s*\{', ('android {' + "`r`n" + $injectTop + "`r`n")
    }

    if ($g -notmatch 'signingConfigs\s*\{\s*release') {
      $signing = @"
    signingConfigs {
        release {
            storeFile keystoreProperties['storeFile'] ? file(keystoreProperties['storeFile']) : null
            storePassword keystoreProperties['storePassword']
            keyAlias keystoreProperties['keyAlias']
            keyPassword keystoreProperties['keyPassword']
        }
    }
"@
      $g = $g -replace '(?s)buildTypes\s*\{', ($signing + "`r`n" + '    buildTypes {')
    }

    if ($g -notmatch 'buildTypes\s*\{[^}]*release\s*\{[^}]*signingConfig\s+signingConfigs\.release') {
      $g = $g -replace '(?s)(buildTypes\s*\{[^}]*release\s*\{)', ('$1' + "`r`n            signingConfig signingConfigs.release")
    }

    $g | Set-Content -Path $gradlePath -Encoding UTF8
  } else {
    Write-Warning "Missing $gradlePath"
  }
} else {
  Write-Host "SkipKeystore enabled - keystore/signing not modified."
}

Write-Host "Step 6/6: Build AAB (flutter build appbundle --release)"
Push-Location $AppDir
try {
  flutter build appbundle --release | Out-Host
  $aab = Join-Path (Get-Location) 'build\app\outputs\bundle\release\app-release.aab'
  Write-Host "AAB ready: $aab"
  Write-Host "Upload this file to Play Console (if using Play App Signing)."
} finally { Pop-Location }

Write-Host "Done."
