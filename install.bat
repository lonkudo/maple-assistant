@echo off
title Maple Assistant Installer
cd /d "%~dp0"

echo.
echo ============================================
echo   Maple Assistant Installer
echo ============================================
echo.
echo   Setting up, please wait...
echo.

rem Remove the zip/download "SmartScreen" block flag from the scripts.
powershell -NoProfile -Command "Get-ChildItem -Path '%~dp0' -Filter '*.ps1' | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

rem Let PowerShell find and run the installer script itself (the Chinese
rem filename must never cross the cmd -> powershell encoding boundary).
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $p = '%~dp0'; $s = Get-ChildItem -Path $p -Filter '*.ps1' | Where-Object { $_.BaseName -ne 'build_release' } | Select-Object -First 1; if (-not $s) { Write-Host 'Installer script not found. Did you EXTRACT the whole folder first?'; exit 1 }; & $s.FullName; exit $LASTEXITCODE }"
if errorlevel 1 (
    echo.
    echo ============================================
    echo   Installation FAILED. Please screenshot the
    echo   red error above and send it to the developer.
    echo ============================================
    pause
)
