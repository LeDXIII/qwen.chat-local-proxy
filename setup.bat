@echo off
REM Setup script for Qwen Local Proxy
echo ========================================
echo Qwen Local Proxy - Setup
echo ========================================
echo.

REM The project is the folder this script lives in, never the caller's folder
cd /d "%~dp0"

REM .env is machine-specific (it holds the Chrome path and the browser profile),
REM so it is never committed. An existing .env is never touched, only created.
if exist ".env" goto env_ready
if not exist ".env.example" (
    echo ERROR: .env is missing and so is .env.example - the checkout is incomplete.
    pause
    exit /b 1
)
echo Creating .env from .env.example...
copy /y ".env.example" ".env" >nul
if not exist ".env" (
    echo ERROR: could not create .env - check that this folder is writable.
    pause
    exit /b 1
)
:env_ready

REM Find a Python launcher: PATH first, then the Windows py launcher, which is
REM present even when Python was installed without the "Add to PATH" checkbox
set "PY=python"
%PY% --version >nul 2>&1
if %errorlevel% neq 0 set "PY=py -3"
%PY% --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found. Please install Python 3.10+
    pause
    exit /b 1
)

%PY% -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ required'" >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python 3.10+ required
    pause
    exit /b 1
)

REM A venv copied from another PC is dead weight: pyvenv.cfg still points at the
REM interpreter it was built with. So the venv is asked whether it runs instead
REM of trusting that the folder exists.
if not exist "venv\Scripts\python.exe" goto make_venv
venv\Scripts\python.exe -c "import sys" >nul 2>&1
if %errorlevel% equ 0 goto venv_ready
echo ERROR: the existing venv does not run on this machine.
echo        A venv is not portable between PCs - delete the venv folder
echo        and run setup.bat again.
pause
exit /b 1

:make_venv
echo Creating virtual environment...
%PY% -m venv venv
if exist "venv\Scripts\python.exe" goto venv_ready
echo ERROR: could not create the venv - see the message above
pause
exit /b 1

:venv_ready
REM Install with the venv interpreter by name. Relying on activate.bat plus a
REM bare pip meant a failed activation silently installed into the global Python.
echo Installing dependencies...
venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
if %errorlevel% neq 0 (
    echo ERROR: dependency install failed - check the network connection.
    pause
    exit /b 1
)

REM Chrome is needed at run time; saying it here beats failing on first request
if not exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    echo NOTE: Chrome was not found in its default location. If it lives
    echo       elsewhere, set QWEN_CHROME_PATH in .env before the first request.
)

echo.
echo ========================================
echo Setup complete!
echo ========================================
echo.
echo Next steps:
echo 1. Run start.bat - the API server starts; the first request
echo    opens the proxy Chrome window automatically
echo 2. Log in to chat.qwen.ai ONCE in that Chrome window - the session
echo    is saved to browser_profile and persists across restarts
echo 3. Run gui.bat to open the chat GUI
echo.
pause
