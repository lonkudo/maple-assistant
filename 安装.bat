@echo off
chcp 65001 >nul
rem 双击本文件即可一键安装 Maple 助手。
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "安装.ps1"
if errorlevel 1 (
    echo.
    echo 安装失败，请把上面的红色报错信息截图发给开发者。
    pause
)
