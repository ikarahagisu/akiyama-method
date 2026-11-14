@echo off
REM v3 backtest: market-wide and risk filters vs v2, sizings 3x100 and 10x30.
REM Takes roughly 20-40 minutes. Output: data\derived\backtest\v3_<date_time>\
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama -v backtest-v3
pause
