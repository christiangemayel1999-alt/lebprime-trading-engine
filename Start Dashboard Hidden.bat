@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if exist ".venv\Scripts\pythonw.exe" (
    start "" /min ".venv\Scripts\pythonw.exe" scripts\run_worker.py
    start "" /min ".venv\Scripts\pythonw.exe" scripts\run_dashboard.py
    goto end
)

if exist "venv\Scripts\pythonw.exe" (
    start "" /min "venv\Scripts\pythonw.exe" scripts\run_worker.py
    start "" /min "venv\Scripts\pythonw.exe" scripts\run_dashboard.py
    goto end
)

if exist "C:\Python314\pythonw.exe" (
    start "" /min "C:\Python314\pythonw.exe" scripts\run_worker.py
    start "" /min "C:\Python314\pythonw.exe" scripts\run_dashboard.py
    goto end
)

echo No pythonw.exe launcher was found. Use "Start Dashboard.bat" instead.

:end
endlocal
