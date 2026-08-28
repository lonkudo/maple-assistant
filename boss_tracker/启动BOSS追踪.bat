@echo off
setlocal
cd /d "%~dp0"

py -3.10 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    py -3.10 "%~dp0app.py"
    if errorlevel 1 goto failed
    goto done
)

python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0app.py"
    if errorlevel 1 goto failed
    goto done
)

:failed
echo.
echo BOSS Tracker could not start. Install Python 3.10 or review the error above.
pause

:done
endlocal
