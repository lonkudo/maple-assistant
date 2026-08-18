@echo off
rem Maple Assistant launcher - runs the assistant with its virtual env.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment missing. Run install.ps1 first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" assistant.py %*
if errorlevel 1 pause
