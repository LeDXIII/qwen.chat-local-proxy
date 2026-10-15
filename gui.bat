@echo off
REM Start the GUI
echo ========================================
echo Qwen Local Proxy - Starting GUI
echo ========================================
echo.

REM Same portability rule as start.bat: the script's own folder is the project
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo ERROR: no venv in %cd%
    echo        Run setup.bat first - it creates the virtual environment.
    pause
    exit /b 1
)

REM PYTHONPATH must be the project root so gui\app.py can import src.*
set "PYTHONPATH=%cd%"
venv\Scripts\python.exe gui\app.py

pause
