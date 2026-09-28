@echo off
rem Ask for administrator permission before doing any installation work.
rem The elevated copy receives --elevated so it does not request UAC again.
if /I "%~1"=="--elevated" goto elevated

net session >nul 2>&1
if not errorlevel 1 goto elevated

rem The unelevated BAT exists only to trigger UAC.  It must close right away;
rem the elevated copy below is the only installer window the user should see.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs" >nul 2>&1
exit /b

:elevated
title TodoHelper Installer
cd /d "%~dp0"

echo.
echo ============================================
echo   TodoHelper Installer
echo ============================================
echo.
echo   One-click setup (YOLO monster detection temporarily disabled).
echo   This package installs its own CPU or CUDA tracking environment.
echo   Please wait, this can take several minutes...
echo.

rem Remove the zip/download "SmartScreen" block flag from the scripts.
powershell -NoProfile -Command "Get-ChildItem -Path '%~dp0' -Filter '*.ps1' | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

rem Run the installer: finds/installs Python, creates the package environment, installs
rem base dependencies and generates launchers.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ============================================
if "%EXIT_CODE%"=="0" (
    echo   Installation finished. Press any key to close this window.
) else (
    echo   Installation FAILED - error code %EXIT_CODE%.
    echo   Please screenshot the red error above and send it to the developer.
)
echo ============================================
echo.
pause
exit /b %EXIT_CODE%
