@echo off
REM Same v2/v3 backtest restricted to the 8 legacy stocks of the earlier research.
REM Run 1: with the eligibility filter. Run 2: without it (as in the earlier research).
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama backtest-v3 --codes 3388,3923,6161,6941,7453,7545,7564,8174
.venv\Scripts\python -m akiyama backtest-v3 --codes 3388,3923,6161,6941,7453,7545,7564,8174 --ignore-eligibility
pause
