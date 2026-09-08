@echo off
setlocal EnableExtensions

REM Start the MT5 trading bot for Windows VPS use.
REM If a local virtual environment exists, activate it first.

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_DIR=%%~fI"
set "LOG_DIR=%PROJECT_DIR%\logs"
set "LAUNCHER_LOG=%LOG_DIR%\launcher.log"
set "PYTHON_CMD="
set "PYTHON_DESC="

if not exist "%LOG_DIR%" (
    mkdir "%LOG_DIR%"
)

call :resolve_python
if not defined PYTHON_CMD (
    echo [%DATE% %TIME%] ERROR: No supported Python interpreter was found.>> "%LAUNCHER_LOG%"
    endlocal
    exit /b 1
)

if not exist "%PROJECT_DIR%\main.py" (
    echo [%DATE% %TIME%] ERROR: main.py not found in %PROJECT_DIR%.>> "%LAUNCHER_LOG%"
    endlocal
    exit /b 1
)

%PYTHON_CMD% -c "import MetaTrader5, pandas, numpy, requests, dotenv" >nul 2>&1
if errorlevel 1 (
    echo [%DATE% %TIME%] ERROR: Python preflight failed for %PYTHON_DESC%.>> "%LAUNCHER_LOG%"
    endlocal
    exit /b 1
)

cd /d "%PROJECT_DIR%"
echo [%DATE% %TIME%] Launching bot with %PYTHON_DESC% >> "%LAUNCHER_LOG%"
%PYTHON_CMD% scripts\bootstrap_launcher.py --wait-seconds 45
if errorlevel 1 (
    echo [%DATE% %TIME%] ERROR: MT5/dashboard bootstrap failed.>> "%LAUNCHER_LOG%"
    endlocal
    exit /b 1
)
%PYTHON_CMD% main.py >> "%LOG_DIR%\bot_stdout.log" 2>&1

endlocal
exit /b %ERRORLEVEL%

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
