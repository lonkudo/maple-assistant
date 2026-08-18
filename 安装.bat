@echo off
title Maple Assistant Installer
cd /d "%~dp0"

echo.
echo ============================================
echo   Maple Assistant Installer
echo ============================================
echo.
echo   One-click setup (includes YOLO detection deps).
echo   Please wait, this can take several minutes...
echo.

rem Remove the zip/download "SmartScreen" block flag from the scripts.
powershell -NoProfile -Command "Get-ChildItem -Path '%~dp0' -Filter '*.ps1' | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

rem Run the installer: finds/installs Python, creates .venv, installs
rem base + YOLO dependencies, generates the launchers. Everything automatic.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo.
    echo ============================================
    echo   Installation FAILED. Please screenshot the
    echo   red error above and send it to the developer.
    echo ============================================
    pause
)
