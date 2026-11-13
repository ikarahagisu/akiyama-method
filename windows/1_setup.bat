@echo off
REM One-time setup: creates .venv, installs the app, opens .env for the API key.
cd /d "%~dp0\.."

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python launcher "py" not found. Install Python 3.12 from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH" during install. Then run this file again.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating virtual environment...
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo [ERROR] Could not create .venv. Python 3.11 or newer is required.
  pause
  exit /b 1
)

echo Installing packages (first time takes a few minutes)...
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"
if errorlevel 1 (
  echo [ERROR] Package install failed. See messages above.
  pause
  exit /b 1
)

echo Running self-test...
.venv\Scripts\python -m pytest -q

if not exist .env copy .env.example .env >nul
echo.
echo ============================================================
echo  Notepad will open .env
echo  Write your key after JQUANTS_API_KEY=   then save and close.
echo ============================================================
notepad .env
echo Setup finished. Next: double-click 2_download_all.bat
pause
