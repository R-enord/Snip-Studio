@echo off
rem Keeps the SnipRiot worker running. Started hidden at sign-in by the "SnipRiot Recorder" task.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "%USERPROFILE%\SnipRiot\data\logs" mkdir "%USERPROFILE%\SnipRiot\data\logs"
:loop
py -3 -u run.py >> "%USERPROFILE%\SnipRiot\data\logs\worker-console.log" 2>&1
timeout /t 30 /nobreak >nul
goto loop
