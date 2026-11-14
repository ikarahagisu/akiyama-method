@echo off
REM v4 company-quality filter vs v2/v3 on the universe without the legacy 8 (evaluate once).
REM Takes roughly 20-40 minutes. Output: data\derived\backtest\v4_<date_time>\
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama -v backtest-v4
pause
