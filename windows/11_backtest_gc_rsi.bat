@echo off
REM Golden cross (SMA5/25) + RSI14 strategy: TP +5%, SL -3%, max 20 days, vs TOPIX.
REM Output: data\derived\backtest\gc_rsi_<date_time>\
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama -v backtest-gc-rsi
pause
