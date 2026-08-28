@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.10 app.py
if errorlevel 1 (
    echo.
    echo BOSS 追踪启动失败。请确认已安装 Python 3.10。
    pause
)
