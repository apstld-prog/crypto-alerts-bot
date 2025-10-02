@echo off
setlocal enabledelayedexpansion

echo ================================================
echo  Step 0: Go to repo root (this script's folder)
echo ================================================
cd /d "%~dp0"

echo.
echo ================================================
echo  Step 1: Enable long paths (harmless if set)
echo ================================================
git config --global core.longpaths true

echo.
echo ================================================
echo  Step 2: Ensure dotfiles (.gitignore/.gitattributes)
echo ================================================
REM If you accidentally have files without leading dot, rename them
if exist "gitattributes" (
  echo Renaming gitattributes -> .gitattributes
  ren "gitattributes" ".gitattributes"
)
if exist "gitignore" (
  echo Renaming gitignore -> .gitignore
  ren "gitignore" ".gitignore"
)

REM Create .gitignore if missing
if not exist ".gitignore" (
  echo Creating .gitignore
  > ".gitignore" echo # root .gitignore
)

REM Create .gitattributes if missing
if not exist ".gitattributes" (
  echo Creating .gitattributes
  > ".gitattributes" echo * text=auto eol=lf
)

REM Append the big folder ignore if not present
findstr /C:"cryptoalerts77_full_project_pack_signed/" ".gitignore" >nul 2>&1
if errorlevel 1 (
  echo Appending "cryptoalerts77_full_project_pack_signed/" to .gitignore
  echo cryptoalerts77_full_project_pack_signed/>>".gitignore"
)

echo.
echo ================================================
echo  Step 3: Commit ONLY the dotfiles first
echo ================================================
git add ".gitignore" ".gitattributes"
git commit -m "Repo hygiene: add/rename .gitignore & .gitattributes and ignore mobile pack"

echo.
echo ================================================
echo  Step 4: Remove mobile pack from index (ignore if not tracked)
echo ================================================
git rm -r --cached "cryptoalerts77_full_project_pack_signed" 2>nul

echo.
echo ================================================
echo  Step 5: Stage ONLY backend files (no add -A)
echo ================================================
REM === Add the files you actually want to push ===
if exist "requirements.txt" git add requirements.txt
if exist "server_combined.py" git add server_combined.py
if exist "start.sh" git add start.sh
if exist "README_ACCESS.md" git add README_ACCESS.md
if exist "README_CUSTOM.md" git add README_CUSTOM.md
if exist "README_FULL.md" git add README_FULL.md

echo.
echo ================================================
echo  Step 6: Commit backend changes (if any)
echo ================================================
git commit -m "Backend update: access model + trial + menu" 

echo.
echo ================================================
echo  Step 7: Push to origin/main
echo ================================================
git push -u origin main

echo.
echo Done. Press any key to close.
pause >nul
