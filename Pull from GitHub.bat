@echo off
setlocal EnableExtensions EnableDelayedExpansion

pushd "%~dp0"

set "PROJECT_DIR=%CD%"
set "REMOTE_URL=https://github.com/christiangemayel1999-alt/trading-bot.git"
set "BRANCH="

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
    echo This folder is not a Git repository yet.
    echo Expected to find: %PROJECT_DIR%\.git
    echo.
    echo If you want this script to work here, clone or initialize the repo first.
    pause
    popd
    endlocal
    exit /b 1
)

for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "BRANCH=%%B"
if not defined BRANCH set "BRANCH=main"

git remote get-url origin >nul 2>nul
if errorlevel 1 (
    git remote add origin "%REMOTE_URL%" >nul
    if errorlevel 1 goto :git_failed
) else (
    git remote set-url origin "%REMOTE_URL%" >nul
    if errorlevel 1 goto :git_failed
)

echo Fetching latest changes from origin...
git fetch origin
if errorlevel 1 goto :git_failed

echo Overwriting local files with origin/%BRANCH%...
git reset --hard origin/%BRANCH%
if errorlevel 1 goto :git_failed

echo Removing untracked and ignored files...
git clean -fdx
if errorlevel 1 goto :git_failed

echo.
echo Full overwrite complete.
pause
popd
endlocal
exit /b 0

:git_failed
echo.
echo Git pull failed.
echo Remote: %REMOTE_URL%
echo Branch : %BRANCH%
echo If the branch name differs on the remote, update the script or pull manually.
pause
popd
endlocal
exit /b 1
