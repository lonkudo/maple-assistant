<#
.SYNOPSIS
    Maple 助手 一键安装脚本。

.DESCRIPTION
    在一台全新的 Windows 电脑上自动完成环境搭建：
      1. 查找本机已有的 Python 3.10-3.12（优先使用你的环境），
         找不到时自动下载并安装 Python 3.12（先尝试 winget，失败则从
         python.org 静默安装）。
      2. 创建本地虚拟环境 (.venv)。
      3. 安装依赖库。
      4. 生成启动器（start_assistant.bat / 启动助手.bat）。

    可选：加 -Yolo 参数创建 YOLO 怪物检测环境
    （yolo-detection\venv313，需要下载 PyTorch，体积较大，约数 GB）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File 安装.ps1
    powershell -ExecutionPolicy Bypass -File 安装.ps1 -Yolo
    powershell -ExecutionPolicy Bypass -File 安装.ps1 -Python C:\Python312\python.exe
#>
param(
    [switch]$Yolo,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Maple 助手 安装程序" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

function Find-Python {
    <#
    返回可用的 Python 3.10-3.12 解释器路径；找不到返回 $null。
    #>
    if ($Python) {
        if (-not (Test-Path $Python)) { throw "未找到 Python: $Python" }
        return (Resolve-Path $Python).Path
    }
    # 1) py 启动器（用户级安装的 Python 3.10-3.12）。
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($ver in "3.12", "3.11", "3.10") {
            $exe = (& py -$ver -c "import sys;print(sys.executable)" 2>$null)
            if ($exe -and (Test-Path $exe)) { return $exe }
        }
    }
    # 2) PATH 中的 python（仅 3.10-3.12，忽略 WindowsApps 占位程序）。
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source -notmatch "WindowsApps") {
        $ver = (& $pythonCmd.Source -c "import sys;print('{0}.{1}'.format(*sys.version_info[:2]))" 2>$null)
        if ($ver -match "^(3\.(10|11|12))$") { return $pythonCmd.Source }
    }
    # 3) 常见安装位置。
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:ProgramFiles\Python312", "$env:ProgramFiles\Python311",
        "$env:ProgramFiles\Python310",
        "C:\Python312", "C:\Python311", "C:\Python310"
    )
    foreach ($dir in $candidates) {
        if (Test-Path $dir) {
            $found = Get-ChildItem $dir -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($found) { return $found.FullName }
        }
    }
    return $null
}

function Install-Python {
    <#
    为当前用户安装 Python 3.12。先尝试 winget，再回退到 python.org
    静默安装程序。返回安装后的 python.exe 路径。
    #>
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "未找到 Python - 正在通过 winget 安装 Python 3.12..." -ForegroundColor Yellow
        & winget install --id Python.Python.3.12 -e --silent `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            $exe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
            if (Test-Path $exe) { return $exe }
        }
    }
    Write-Host "正在从 python.org 下载 Python 3.12..." -ForegroundColor Yellow
    $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    $installer = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
    Invoke-WebRequest -Uri $url -OutFile $installer
    $quietArgs = "/quiet InstallAllUsers=0 PrependPath=0 Include_test=0 " +
        "Include_launcher=1 AssociateFiles=0 Shortcuts=0 SimpleInstall=1"
    $process = Start-Process -FilePath $installer -ArgumentList $quietArgs -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Python 安装程序退出码为 $($process.ExitCode)"
    }
    $exe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-Path $exe)) {
        throw "Python 已安装但找不到 python.exe；请手动安装 Python 3.10-3.12 后重试"
    }
    return $exe
}

# ---- 1. Python -------------------------------------------------------------
$python = Find-Python
if (-not $python) { $python = Install-Python }
Write-Host "使用 Python: $python" -ForegroundColor Green
& $python -c "import sys; print('  版本:', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw "Python 检查失败" }

# ---- 2. 虚拟环境 ------------------------------------------------------------
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "正在创建虚拟环境 .venv ..." -ForegroundColor Yellow
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "虚拟环境创建失败" }
}
Write-Host "虚拟环境已就绪。"

# ---- 3. 安装依赖 -------------------------------------------------------------
Write-Host "正在安装依赖 (numpy, Pillow, OpenCV, pywin32) ..." -ForegroundColor Yellow
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip 安装失败" }

# ---- 4. 生成启动器 ------------------------------------------------------------
$bat = @"
@echo off
rem Maple 助手启动器 - 使用虚拟环境运行助手。
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 缺少虚拟环境，请先运行 安装.ps1。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" assistant.py %*
if errorlevel 1 pause
"@
Set-Content -Path "start_assistant.bat" -Value $bat -Encoding ASCII
Set-Content -Path (Join-Path $root "启动助手.bat") -Value $bat -Encoding Default
Write-Host "启动器已生成: start_assistant.bat / 启动助手.bat" -ForegroundColor Green

# ---- 5. 可选 YOLO 环境 --------------------------------------------------------
if ($Yolo) {
    Write-Host ""
    Write-Host "正在创建 YOLO 怪物检测环境（torch - 需要下载" -ForegroundColor Yellow
    Write-Host "数 GB，耗时较长）..." -ForegroundColor Yellow
    $yoloVenv = Join-Path $root "yolo-detection\venv313\Scripts\python.exe"
    if (-not (Test-Path $yoloVenv)) {
        & $python -m venv "yolo-detection\venv313"
        if ($LASTEXITCODE -ne 0) { throw "YOLO 虚拟环境创建失败" }
    }
    & $yoloVenv -m pip install --upgrade pip
    & $yoloVenv -m pip install -r "yolo-detection\requirements.txt"
    if ($LASTEXITCODE -ne 0) { throw "YOLO pip 安装失败" }
    Write-Host "YOLO 环境已就绪 (yolo-detection\venv313)。" -ForegroundColor Green
    Write-Host "请将训练好的模型放到 yolo-detection\weights\best.pt" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  安装完成。" -ForegroundColor Cyan
Write-Host "  双击 启动助手.bat（或 start_assistant.bat）即可开始。" -ForegroundColor Cyan
if (-not $Yolo) {
    Write-Host "  如需 YOLO 怪物检测，请重新双击 安装.bat 并加 -Yolo 参数：" -ForegroundColor Cyan
    Write-Host "    powershell -ExecutionPolicy Bypass -File 安装.ps1 -Yolo" -ForegroundColor Cyan
}
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
