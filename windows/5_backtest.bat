@echo off
REM Backtest of chronos_v2 candidates (run after 4_build_history.bat).
REM Results are saved under data\derived\backtest\<date_time>\ and summarised on screen.
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama backtest
pause
