@echo off
REM How accurate must an AI risk review be to beat TOPIX? (hypothetical, no API calls)
REM Takes roughly 30-60 minutes. Output: data\derived\backtest\ai_sensitivity_<date_time>\
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama -v ai-sensitivity
pause
