@echo off
title BOSS Tracker Setup
cd /d "%~dp0"

echo.
echo ============================================
echo   BOSS Tracker One-Click Setup
echo ============================================
echo.
echo This will install Python 3.10 if needed,
echo create the local environment, and install all packages.
echo Please keep this window open until it says complete.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_boss_tracker.ps1"
if errorlevel 1 (
    echo.
    echo ============================================
    echo Setup FAILED. Please screenshot this window
    echo and send it to the developer.
    echo ============================================
    pause
)
