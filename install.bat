@echo off
title Maple 助手 - 一键安装
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================
echo   Maple 助手 一键安装
echo ============================================
echo.
echo   正在准备安装，请稍候...
echo.

rem 解除压缩包安全标记（来自网络/压缩包的 SmartScreen 提示）。
powershell -NoProfile -Command "Get-ChildItem -Path '%~dp0' -Filter '*.ps1' | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

rem 用通配符查找安装脚本，避免中文文件名在不同编码下的解析问题。
set "INSTALL_SCRIPT="
for %%F in ("%~dp0*.ps1") do (
    if /I not "%%~nF"=="build_release" set "INSTALL_SCRIPT=%%~fF"
)
if not defined INSTALL_SCRIPT (
    echo 找不到安装脚本，请确认已【全部解压】整个文件夹后再运行本文件。
    echo 如果在压缩包内部直接双击，请先右键压缩包选择“全部解压”。
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_SCRIPT%"
if errorlevel 1 (
    echo.
    echo ============================================
    echo   安装失败。请把窗口中的红色报错信息截图发给开发者。
    echo ============================================
    pause
)
