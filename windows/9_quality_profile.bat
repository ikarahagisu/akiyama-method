@echo off
REM Company-quality features: legacy 8 vs universe, and dev-period outcome buckets.
REM Output: data\derived\quality\quality_profile.csv and quality_dev_buckets.csv
cd /d "%~dp0\.."
.venv\Scripts\python -m akiyama quality-profile
pause
