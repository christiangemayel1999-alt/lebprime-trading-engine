@echo off
setlocal EnableExtensions EnableDelayedExpansion

pushd "%~dp0"

set "PROJECT_DIR=%CD%"
set "REMOTE_URL=https://github.com/christiangemayel1999-alt/trading-bot.git"
set "BRANCH="
set "COMMIT_MESSAGE=Auto backup %DATE% %TIME%"
set "IS_NEW_REPO="

where git >nul 2>nul
if errorlevel 1 (
    echo Git was not found on this PC.
    echo Install Git for Windows, then run this file again.
    pause
    popd
    endlocal
    exit /b 1
)

if not exist ".git" (
    echo Initializing Git repository...
    git init >nul
    if errorlevel 1 goto :git_failed
    set "IS_NEW_REPO=1"
    git branch -M main >nul 2>nul
)

for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "BRANCH=%%B"
if not defined BRANCH set "BRANCH=main"
if defined IS_NEW_REPO set "BRANCH=main"

git remote get-url origin >nul 2>nul
if errorlevel 1 (
    git remote add origin "%REMOTE_URL%" >nul
    if errorlevel 1 goto :git_failed
) else (
    git remote set-url origin "%REMOTE_URL%" >nul
    if errorlevel 1 goto :git_failed
)

echo Staging files...
git add -A
if errorlevel 1 goto :git_failed

for /f "delims=" %%S in ('git status --porcelain') do set "HAS_CHANGES=1"

if not defined HAS_CHANGES (
    echo No new changes to commit.
) else (
    echo Committing changes...
    git commit -m "%COMMIT_MESSAGE%" >nul
    if errorlevel 1 goto :git_failed
)

echo Pushing to GitHub...
git push -u origin %BRANCH%
if errorlevel 1 goto :git_failed

echo.
echo Push complete.
pause
popd
endlocal
exit /b 0

:git_failed
echo.
echo Git push failed.
echo If this is the first push, make sure you are signed in to GitHub from Git and that the remote repository exists.
echo Remote: %REMOTE_URL%
echo Branch : %BRANCH%
pause
popd
endlocal
exit /b 1
