@echo off
rem Ask for administrator permission before doing any installation work.
rem The elevated copy receives --elevated so it does not request UAC again.
if /I "%~1"=="--elevated" goto elevated
net session >nul 2>&1
if not errorlevel 1 goto elevated
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
if errorlevel 1 (
    echo Administrator permission was not granted. Installation cancelled.
    pause
)
exit /b

:elevated
title Maple Assistant Installer
cd /d "%~dp0"

echo.
echo ============================================
echo   Maple Assistant Installer
echo ============================================
echo.
echo   One-click setup (YOLO monster detection temporarily disabled).
echo   Please wait, this can take several minutes...
echo.

rem Remove the zip/download "SmartScreen" block flag from the scripts.
powershell -NoProfile -Command "Get-ChildItem -Path '%~dp0' -Filter '*.ps1' | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

rem Run the installer: finds/installs Python, creates .venv, installs
rem Installs base dependencies and generates launchers. YOLO dependency
rem download is temporarily disabled in install.ps1; see README.md to restore.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo.
    echo ============================================
    echo   Installation FAILED. Please screenshot the
    echo   red error above and send it to the developer.
    echo ============================================
    pause
)
