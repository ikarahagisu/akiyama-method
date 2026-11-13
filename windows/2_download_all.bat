@echo off
REM First full download of all available history (about 800 MB, may take 1-2 hours).
REM Safe to re-run: files already downloaded are skipped.
cd /d "%~dp0\.."
if not exist .venv\Scripts\python.exe (
  echo [ERROR] Run 1_setup.bat first.
  pause
  exit /b 1
)
echo [1/4] J-Quants bulk files...
.venv\Scripts\python -m akiyama sync-bulk
echo [2/4] TDnet disclosure index...
.venv\Scripts\python -m akiyama sync-td
echo [3/4] Building tables...
.venv\Scripts\python -m akiyama normalize
echo [4/4] Status
.venv\Scripts\python -m akiyama status
echo.
echo Done. If any line above shows errors, just run this file again.
pause
