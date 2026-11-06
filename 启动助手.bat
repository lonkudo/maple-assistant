@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Virtual environment missing. Run the setup first.
    pause
    exit /b 1
)
rem Start the hidden, elevated launcher and close this console immediately.
rem Running wscript.exe directly kept this cmd window on screen while the UAC
rem prompt was up and it stayed visible after the confirmation, so users read it
rem as something harmful.  "start" detaches: this window is gone before UAC.
start "" wscript.exe //nologo "%~dp0launch_assistant.vbs" %*
exit /b
