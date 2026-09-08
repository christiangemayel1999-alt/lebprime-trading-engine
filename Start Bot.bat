@echo off
setlocal EnableExtensions

pushd "%~dp0"

set "PROJECT_DIR=%CD%"
set "LOG_DIR=%PROJECT_DIR%\logs"
set "LAUNCHER_LOG=%LOG_DIR%\launcher.log"
set "PYTHON_CMD="
set "PYTHON_DESC="
set "AUTO_START=1"
set "DASHBOARD_URL=http://127.0.0.1:8501"

if /I "%~1"=="--dashboard-only" set "AUTO_START=0"
if /I "%~1"=="/dashboard-only" set "AUTO_START=0"
if /I "%~1"=="--no-autostart" set "AUTO_START=0"
if /I "%~1"=="/no-autostart" set "AUTO_START=0"
if /I "%~1"=="--autostart" set "AUTO_START=1"
if /I "%~1"=="/autostart" set "AUTO_START=1"

if not exist "%LOG_DIR%" (
    mkdir "%LOG_DIR%"
)

call :resolve_python
if not defined PYTHON_CMD (
    echo [%DATE% %TIME%] ERROR: No working Python interpreter was found.>> "%LAUNCHER_LOG%"
    echo.
    echo ERROR: No supported Python interpreter was found.
    echo Install Python 3.11 or create a local virtual environment in `.venv` or `venv`.
    echo.
    pause
    popd
    endlocal
    exit /b 1
)

if not exist "%PROJECT_DIR%\main.py" (
    echo [%DATE% %TIME%] ERROR: main.py not found in %PROJECT_DIR%.>> "%LAUNCHER_LOG%"
    echo.
    echo ERROR: main.py was not found.
    echo Expected path: %PROJECT_DIR%\main.py
    echo.
    pause
    popd
    endlocal
    exit /b 1
)

if not exist "%PROJECT_DIR%\config.json" (
    echo [%DATE% %TIME%] ERROR: config.json not found in %PROJECT_DIR%.>> "%LAUNCHER_LOG%"
    echo.
    echo ERROR: config.json was not found.
    echo.
    pause
    popd
    endlocal
    exit /b 1
)

title Trading Bot
echo ========================================
echo Trading Bot Launcher (Dashboard + Bot)
echo Project: %PROJECT_DIR%
echo Python : %PYTHON_DESC%
echo Logs   : %LOG_DIR%
echo ========================================
echo.
if not exist "%PROJECT_DIR%\.env" (
    echo WARNING: .env file was not found in the project folder.
    echo The bot may fail if MT5 credentials are only stored there.
    echo.
)

%PYTHON_CMD% -c "import MetaTrader5, pandas, numpy, requests, dotenv; print('Launcher preflight OK')" >nul 2>&1
if errorlevel 1 (
    echo [%DATE% %TIME%] ERROR: Python preflight failed for %PYTHON_DESC%.>> "%LAUNCHER_LOG%"
    echo.
    echo ERROR: Python started, but required packages failed to import.
    echo Python: %PYTHON_DESC%
    echo Try installing requirements into the selected interpreter or use a local virtual environment.
    echo.
    pause
    popd
    endlocal
    exit /b 1
)

echo Preparing MT5 and dashboard...
echo [%DATE% %TIME%] Launching MT5/dashboard bootstrap >> "%LAUNCHER_LOG%"
%PYTHON_CMD% scripts\bootstrap_launcher.py --wait-seconds 45
if errorlevel 1 (
    echo [%DATE% %TIME%] ERROR: MT5/dashboard bootstrap failed.>> "%LAUNCHER_LOG%"
    echo.
    echo ERROR: MT5 could not be launched or detected.
    echo Check mt5.terminal_path or the MT5_PATH environment variable.
    echo.
    pause
    popd
    endlocal
    exit /b 1
)

if "%AUTO_START%"=="1" goto :autostart

echo.
echo Dashboard and MT5 bootstrap completed.
echo MT5 is running and the dashboard should now be available at:
echo   %DASHBOARD_URL%
echo.
echo Bot was NOT auto-started because you passed --dashboard-only.
echo Open the dashboard and use the Start Bot button when you are ready.
echo.
echo If you only want dashboard and MT5, run:
echo   Start Bot.bat --dashboard-only
echo.
echo [%DATE% %TIME%] Dashboard-only launcher completed (no bot autostart).>> "%LAUNCHER_LOG%"
pause
set "EXIT_CODE=0"
goto :cleanup

:autostart
echo Starting bot...
echo.
echo [%DATE% %TIME%] Launching bot with %PYTHON_DESC% >> "%LAUNCHER_LOG%"
echo Output will stay in this window while the bot is running.
echo Structured bot logs will still be written to:
echo   logs\bot.log
echo   logs\errors.log
echo Dashboard control state will be written to:
echo   storage\control_state.json
echo.
set "TRADING_MODE=LIVE"
set "BOT_TRADING_MODE=LIVE"
%PYTHON_CMD% main.py
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Bot process exited with code %EXIT_CODE%.
echo Check logs\bot.log and logs\errors.log for details.
echo [%DATE% %TIME%] Bot exited with code %EXIT_CODE%.>> "%LAUNCHER_LOG%"
pause

:cleanup

popd
endlocal
exit /b %EXIT_CODE%

:resolve_python
if defined VIRTUAL_ENV (
    if exist "%VIRTUAL_ENV%\Scripts\python.exe" (
        "%VIRTUAL_ENV%\Scripts\python.exe" -c "import sys" >nul 2>nul
        if not errorlevel 1 (
            set "PYTHON_CMD="%VIRTUAL_ENV%\Scripts\python.exe""
            set "PYTHON_DESC=active virtual environment: %VIRTUAL_ENV%"
            goto :eof
        )
    )
)

if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    "%PROJECT_DIR%\.venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD="%PROJECT_DIR%\.venv\Scripts\python.exe""
        set "PYTHON_DESC=%PROJECT_DIR%\.venv\Scripts\python.exe"
        goto :eof
    )
)

if defined PYTHON_PATH (
    "%PYTHON_PATH%" -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD="%PYTHON_PATH%""
        set "PYTHON_DESC=PYTHON_PATH=%PYTHON_PATH%"
        goto :eof
    )
)

if defined BOT_PYTHON_PATH (
    "%BOT_PYTHON_PATH%" -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD="%BOT_PYTHON_PATH%""
        set "PYTHON_DESC=BOT_PYTHON_PATH=%BOT_PYTHON_PATH%"
        goto :eof
    )
)

if exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
    "%PROJECT_DIR%\venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD="%PROJECT_DIR%\venv\Scripts\python.exe""
        set "PYTHON_DESC=%PROJECT_DIR%\venv\Scripts\python.exe"
        goto :eof
    )
)

py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3.11"
    set "PYTHON_DESC=py -3.11"
    goto :eof
)

py -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    set "PYTHON_DESC=py default launcher fallback"
    goto :eof
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    set "PYTHON_DESC=python PATH fallback"
)
goto :eof
