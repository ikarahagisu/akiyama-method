@echo off
REM After-close update (run once a day, e.g. after 17:00 JST).
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama daily
.venv\Scripts\python -m akiyama status
pause
