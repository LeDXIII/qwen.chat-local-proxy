@echo off
REM Start the API server
echo ========================================
echo Qwen Local Proxy - Starting API Server
echo ========================================
echo.

REM Run against the folder this script lives in: double-clicking works from any
REM drive or shortcut, and the caller's directory stops mattering
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo ERROR: no venv in %cd%
    echo        Run setup.bat first - it creates the virtual environment.
    pause
    exit /b 1
)

REM The venv interpreter by name, not whatever "python" the caller's PATH points
REM at: a global python without fastapi used to fail as "module not found" after
REM a silent activation failure, which reads like a broken install
venv\Scripts\python.exe scripts\run_server.py

pause
