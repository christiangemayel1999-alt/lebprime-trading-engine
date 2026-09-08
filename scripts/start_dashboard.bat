@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
set "PORT=8501"
set "HOST=0.0.0.0"

if defined DASHBOARD_PORT set "PORT=%DASHBOARD_PORT%"
if defined DASHBOARD_HOST set "HOST=%DASHBOARD_HOST%"

if exist "%PROJECT_DIR%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%PROJECT_DIR%\.env") do (
        if /I "%%A"=="DASHBOARD_PORT" set "PORT=%%B"
        if /I "%%A"=="DASHBOARD_HOST" set "HOST=%%B"
    )
)

cd /d "%PROJECT_DIR%"

if defined VIRTUAL_ENV (
    if exist "%VIRTUAL_ENV%\Scripts\python.exe" goto run_virtualenv
)

if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" goto run_dotvenv

if defined PYTHON_PATH (
    "%PYTHON_PATH%" -c "import sys" >nul 2>nul
    if not errorlevel 1 goto run_python_path
)

if defined BOT_PYTHON_PATH (
    "%BOT_PYTHON_PATH%" -c "import sys" >nul 2>nul
    if not errorlevel 1 goto run_bot_python_path
)

if exist "%PROJECT_DIR%\venv\Scripts\python.exe" goto run_venv

py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_py311

py -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_py

python -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_python

echo ERROR: No usable Python interpreter was found.
echo Install Python or create a local virtual environment in .venv or venv.
endlocal
exit /b 1

:run_virtualenv
call :start_worker_exe "%VIRTUAL_ENV%\Scripts\python.exe"
"%VIRTUAL_ENV%\Scripts\python.exe" scripts\run_dashboard.py --host %HOST% --port %PORT%
goto end

:run_dotvenv
call :start_worker_exe "%PROJECT_DIR%\.venv\Scripts\python.exe"
"%PROJECT_DIR%\.venv\Scripts\python.exe" scripts\run_dashboard.py --host %HOST% --port %PORT%
goto end

:run_python_path
call :start_worker_exe "%PYTHON_PATH%"
"%PYTHON_PATH%" scripts\run_dashboard.py --host %HOST% --port %PORT%
goto end

:run_bot_python_path
call :start_worker_exe "%BOT_PYTHON_PATH%"
"%BOT_PYTHON_PATH%" scripts\run_dashboard.py --host %HOST% --port %PORT%
goto end

:run_venv
call :start_worker_exe "%PROJECT_DIR%\venv\Scripts\python.exe"
"%PROJECT_DIR%\venv\Scripts\python.exe" scripts\run_dashboard.py --host %HOST% --port %PORT%
goto end

:run_py311
call :start_worker_py311
py -3.11 scripts\run_dashboard.py --host %HOST% --port %PORT%
goto end

:run_py
call :start_worker_py
py scripts\run_dashboard.py --host %HOST% --port %PORT%
goto end

:run_python
call :start_worker_python
python scripts\run_dashboard.py --host %HOST% --port %PORT%
goto end

:start_worker_exe
if /I "%DISABLE_BACKTEST_WORKER%"=="1" exit /b 0
start "Trading Bot Worker" /min "%~1" scripts\run_worker.py
exit /b 0

:start_worker_py311
if /I "%DISABLE_BACKTEST_WORKER%"=="1" exit /b 0
start "Trading Bot Worker" /min py -3.11 scripts\run_worker.py
exit /b 0

:start_worker_py
if /I "%DISABLE_BACKTEST_WORKER%"=="1" exit /b 0
start "Trading Bot Worker" /min py scripts\run_worker.py
exit /b 0

:start_worker_python
if /I "%DISABLE_BACKTEST_WORKER%"=="1" exit /b 0
start "Trading Bot Worker" /min python scripts\run_worker.py
exit /b 0

:end
endlocal
