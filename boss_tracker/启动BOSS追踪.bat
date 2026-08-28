@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "%~dp0app.py"
    if errorlevel 1 goto failed
    goto done
)

:failed
echo.
echo BOSS Tracker is not installed yet, or could not start.
echo Double-click the installer first, then try again.
pause

:done
endlocal
