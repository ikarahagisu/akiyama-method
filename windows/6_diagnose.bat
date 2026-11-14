@echo off
REM Bucketed diagnostics of the latest backtest (run after 5_backtest.bat).
REM Output: data\derived\backtest\<latest>\diagnostics.csv
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama diagnose
pause
