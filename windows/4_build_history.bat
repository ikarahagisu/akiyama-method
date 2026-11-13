@echo off
REM One-time build of the full history: universe, signals, Chronos-2 forecasts, candidates.
REM Forecasting takes several hours on CPU. It is resumable: if it stops, run this file again.
REM Keep the PC awake (Settings > System > Power: Sleep = Never) while it runs.
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama universe --from 2016-11-01 --to 2099-12-31
.venv\Scripts\python -m akiyama signals --from 2016-11-01 --to 2099-12-31
.venv\Scripts\python -m akiyama forecast --from 2016-11-01 --to 2099-12-31
.venv\Scripts\python -m akiyama candidates --from 2016-11-01 --to 2099-12-31
echo.
echo Done. If forecasting stopped early, run this file again (it continues where it left off).
pause
