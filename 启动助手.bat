@echo off
rem Maple Assistant 启动器 - 使用虚拟环境运行助手。
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 缺少虚拟环境，请先运行 install.ps1。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" assistant.py %*
if errorlevel 1 pause
