@echo off
setlocal enabledelayedexpansion
echo ================================================
echo  Repo hygiene: .gitattributes / .gitignore / push
echo ================================================

REM 1) Go to script directory (repo root)
cd /d "%~dp0"

REM 2) Enable long paths globally (harmless if already set)
git config --global core.longpaths true

REM 3) Rename files without leading dot -> dotfiles
if exist "gitattributes" (
  echo Renaming gitattributes -> .gitattributes
  ren "gitattributes" ".gitattributes"
)
if exist "gitignore" (
  echo Renaming gitignore -> .gitignore
  ren "gitignore" ".gitignore"
)

REM 4) Ensure .gitignore exists
if not exist ".gitignore" (
  echo Creating .gitignore
  > ".gitignore" echo # root .gitignore
)

REM 5) Ensure mobile pack is ignored
findstr /C:"cryptoalerts77_full_project_pack_signed/" ".gitignore" >nul 2>&1
if errorlevel 1 (
  echo Appending "cryptoalerts77_full_project_pack_signed/" to .gitignore
  echo cryptoalerts77_full_project_pack_signed/>>".gitignore"
)

REM 6) Remove folder from index if it was tracked (ignore errors if not tracked)
git rm -r --cached "cryptoalerts77_full_project_pack_signed" 2>nul

REM 7) Stage files (dotfiles + any other current changes you θέλεις)
echo Staging files...
git add -A

REM 8) Commit
echo Committing...
git commit -m "Repo hygiene: enforce LF and ignore mobile pack"

REM 9) Push
echo Pushing to origin main...
git push -u origin main

echo.
echo Done. Press any key to close.
pause >nul
