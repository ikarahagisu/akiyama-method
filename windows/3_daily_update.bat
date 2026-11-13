@echo off
REM After-close update (run once a day, e.g. after 17:00 JST):
REM new data -> eligible universe -> price condition -> Chronos-2 -> candidates (last 40 days).
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama -v daily
.venv\Scripts\python -m akiyama pipeline
.venv\Scripts\python -m akiyama status
pause
