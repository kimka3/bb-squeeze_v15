@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
py run_backtest.py
pause
