@echo off
rem Request administrator rights (UAC) so injected keys reach the game,
rem matching how the assistant ran on the dev machine.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~dp0start_assistant.bat' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Virtual environment missing. Run the setup first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" assistant.py %*
