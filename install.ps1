<#
.SYNOPSIS
    Maple 助手 一键安装脚本（由 安装.bat 调用，用户无需手动运行）。

.DESCRIPTION
    在一台全新的 Windows 电脑上自动完成环境搭建：
      1. 查找本机已有的 Python 3.10-3.12（优先 3.10），
         找不到时自动下载并安装 Python 3.10（先尝试 winget，失败则从
         python.org 静默安装）。
      2. 创建本地虚拟环境 (.venv)（使用 Python 3.10 解释器启动助手）。
      3. 安装基础依赖库（优先阿里云镜像，失败自动回退官方 PyPI）。
      4. 暂不安装 YOLO 怪物检测依赖（模型重新训练后可按 README 恢复）。
      5. 生成启动器（start_assistant.bat / 启动助手.bat）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Python C:\Python312\python.exe
#>
param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
# PowerShell 7.3+ 会把外部命令的 stderr 当作终止错误（配合上面的 Stop）；
# 关闭该行为，让 "py: no such version" 之类的探测自然失败并尝试下一个候选，
# 而不是直接中断安装。
$PSNativeCommandUseErrorActionPreference = $false
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
    # 1) py 启动器（用户级安装的 Python 3.10-3.12；优先 3.10）。
    #    注意：py 探测在没有对应版本时会把 stderr 变成错误记录（配合
    #    $ErrorActionPreference="Stop" 会直接中断），所以每个探测都要 try/catch，
    #    失败就尝试下一个候选。
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($ver in "3.10", "3.11", "3.12") {
            try {
                $exe = (& py -$ver -c "import sys;print(sys.executable)" 2>$null)
                if ($exe -and (Test-Path $exe)) { return $exe }
            } catch {
                # 该版本不存在，尝试下一个。
            }
        }
    }
    # 2) PATH 中的 python（仅 3.10-3.12，忽略 WindowsApps 占位程序）。
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source -notmatch "WindowsApps") {
        try {
            $ver = (& $pythonCmd.Source -c "import sys;print('{0}.{1}'.format(*sys.version_info[:2]))" 2>$null)
            if ($ver -match "^(3\.(10|11|12))$") { return $pythonCmd.Source }
        } catch {
            # 该 python 无法运行或版本不符，继续。
        }
    }
    # 3) 常见安装位置（优先 3.10）。
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:ProgramFiles\Python310", "$env:ProgramFiles\Python311",
        "$env:ProgramFiles\Python312",
        "C:\Python310", "C:\Python311", "C:\Python312"
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
    为当前用户安装 Python 3.10（固定用 3.10，不用最新版）。优先直接运行
    python.org 官方静默安装程序；失败时再回退到 winget。返回 python.exe 路径。
    安装.bat 已在最开始取得管理员权限，所以此处不会再次弹出 UAC 或安装向导。
    #>
    $url = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe"
    $installer = Join-Path $env:TEMP "python-3.10.11-amd64.exe"
    try {
        Write-Host "未找到 Python - 正在后台下载并安装 Python 3.10..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri $url -OutFile $installer
        $quietArgs = "/quiet InstallAllUsers=0 PrependPath=0 Include_test=0 " +
            "Include_pip=1 Include_tcltk=1 Include_launcher=1 AssociateFiles=0 " +
            "Shortcuts=0 SimpleInstall=1"
        $process = Start-Process -FilePath $installer -ArgumentList $quietArgs `
            -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "Python 安装程序退出码为 $($process.ExitCode)"
        }
        $exe = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
        if (Test-Path $exe) { return $exe }
        throw "Python 安装结束后找不到 python.exe"
    } catch {
        Write-Warning "Python 官方静默安装失败：$($_.Exception.Message)"
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "正在通过 winget 后台重试 Python 3.10..." -ForegroundColor Yellow
        & winget install --id Python.Python.3.10 -e --silent --disable-interactivity `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            $exe = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
            if (Test-Path $exe) { return $exe }
            $exe = Find-Python
            if ($exe) { return $exe }
        }
    }
    throw "无法自动安装 Python 3.10；请检查网络后重新双击安装.bat"
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
# 国内优先使用阿里云 PyPI 镜像；若镜像临时不可用，自动回退官方 PyPI。
$pipMirror = "https://mirrors.aliyun.com/pypi/simple/"
Write-Host "正在安装依赖 (numpy, Pillow, OpenCV, pywin32) ..." -ForegroundColor Yellow
& $venvPy -m pip install --disable-pip-version-check --upgrade pip `
    -i $pipMirror --timeout 30 --retries 2
if ($LASTEXITCODE -ne 0) {
    Write-Host "阿里云镜像不可用，正在改用官方 PyPI 更新 pip ..." -ForegroundColor Yellow
    & $venvPy -m pip install --disable-pip-version-check --upgrade pip `
        --index-url https://pypi.org/simple --timeout 45 --retries 2
    if ($LASTEXITCODE -ne 0) { throw "pip 更新失败" }
}
& $venvPy -m pip install --disable-pip-version-check -r requirements.txt `
    -i $pipMirror --timeout 30 --retries 2
if ($LASTEXITCODE -ne 0) {
    Write-Host "阿里云镜像不可用，正在改用官方 PyPI 安装依赖 ..." -ForegroundColor Yellow
    & $venvPy -m pip install --disable-pip-version-check -r requirements.txt `
        --index-url https://pypi.org/simple --timeout 45 --retries 2
    if ($LASTEXITCODE -ne 0) { throw "pip 依赖安装失败" }
}

# ---- 4. 生成启动器 ------------------------------------------------------------
# 启动器内容必须为纯 ASCII：cmd 会用系统代码页解码 .bat，中文会乱码并破坏语法。
# 启动器会先通过 UAC 请求管理员权限（与游戏同权限注入按键才能生效），
# 然后使用 pythonw 启动助手：不显示命令行窗口，只显示图形界面。
$bat = @"
@echo off
rem Request administrator rights (UAC) so injected keys reach the game.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~dp0start_assistant.bat' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Virtual environment missing. Run the setup first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" assistant.py %*
"@
Set-Content -Path "start_assistant.bat" -Value $bat -Encoding ASCII
Set-Content -Path (Join-Path $root "启动助手.bat") -Value $bat -Encoding ASCII
Write-Host "启动器已生成: start_assistant.bat / 启动助手.bat" -ForegroundColor Green

# ---- 5. YOLO 怪物检测依赖（暂时停用；恢复步骤见 README.md） ------------------
<# YOLO_DEPENDENCIES_TEMPORARILY_DISABLED
Write-Host ""
Write-Host "正在安装 YOLO 怪物检测依赖到主环境 .venv ..." -ForegroundColor Yellow
Write-Host "（先安装 CPU 版 PyTorch，下载约 200MB；CUDA 版约 2.5GB）" -ForegroundColor Yellow
& $venvPy -m pip install --upgrade pip -i $pipMirror
# CPU 版 torch/torchvision：检测用 CPU 推理足够（device: auto 会自动选）。
# torch 走官方 CPU 源（普通 PyPI 镜像不提供指定的 CPU wheel 仓库）。
& $venvPy -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
if ($LASTEXITCODE -ne 0) { throw "torch 安装失败" }
# 其余依赖（走阿里云镜像）：跳过 torch/torchvision 行（已装 CPU 版，
# 避免被覆盖成 CUDA 版）。保留 opencv-python（显示检测画面需要 GUI 版）。
$reqs = Get-Content "yolo-detection\requirements.txt" | Where-Object {
    $_ -notmatch '^\s*#' -and $_ -notmatch '^\s*(torch|torchvision)\b'
}
$filtered = Join-Path $env:TEMP "yolo_reqs_filtered.txt"
[System.IO.File]::WriteAllLines($filtered, $reqs, [System.Text.Encoding]::ASCII)
& $venvPy -m pip install -r $filtered -i $pipMirror
if ($LASTEXITCODE -ne 0) { throw "YOLO 依赖安装失败" }
Remove-Item $filtered -ErrorAction SilentlyContinue
Write-Host "YOLO 环境已就绪（主环境 .venv，无需单独的 venv313）。" -ForegroundColor Green
Write-Host "请将训练好的模型放到 yolo-detection\weights\best.pt" -ForegroundColor Yellow
#>
Write-Host "已跳过 YOLO 怪物检测依赖（当前模型识别率不足）。" -ForegroundColor DarkYellow

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  安装完成。" -ForegroundColor Cyan
Write-Host "  双击 启动助手.bat（或 start_assistant.bat）即可开始。" -ForegroundColor Cyan
Write-Host "  首次启动会弹出 UAC 管理员权限确认，请点击“是”。" -ForegroundColor Yellow
Write-Host "  注意：游戏也必须以管理员权限运行，否则按键无法注入。" -ForegroundColor Yellow
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
