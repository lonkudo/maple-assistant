<#
.SYNOPSIS
    TodoHelper 一键安装脚本（由 安装.bat 调用，用户无需手动运行）。

.DESCRIPTION
    在一台全新的 Windows 电脑上自动完成环境搭建：
      1. 查找本机已有的 Python 3.10-3.12（优先 3.10），
         找不到时自动下载并安装 Python 3.10（先尝试 winget，失败则从
         python.org 静默安装）。
      2. 创建本地虚拟环境 .venv。
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
# 测谎与瞄准都由远端 RoiTrack API 完成（测试api / api_lie_video.py）：发布包里没有
# 本地模型，也就不需要 torch，更不需要区分 CPU / CUDA 发布包。助手只有 .venv 一个环境。

# PowerShell 7.3+ 会把外部命令的 stderr 当作终止错误（配合上面的 Stop）；
# 关闭该行为，让 "py: no such version" 之类的探测自然失败并尝试下一个候选，
# 而不是直接中断安装。
$PSNativeCommandUseErrorActionPreference = $false
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvName = ".venv"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  TodoHelper 安装程序" -ForegroundColor Cyan
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
$venvPath = Join-Path $root $venvName
$venvPy = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "正在创建虚拟环境 $venvName ..." -ForegroundColor Yellow
    & $python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "虚拟环境创建失败" }
}
Write-Host "虚拟环境已就绪。"


# ---- 2.5 确保虚拟环境里有 pip ------------------------------------------------
# 旧副本/缺 ensurepip 的 Python 建出的 venv 可能没有 pip，先自愈再装依赖。
& $venvPy -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "虚拟环境缺少 pip，正在修复 ..." -ForegroundColor Yellow
    & $venvPy -m ensurepip --upgrade --default-pip 2>$null | Out-Null
    & $venvPy -m pip --version 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ensurepip 失败，尝试用基础 Python 重建虚拟环境 ..." -ForegroundColor Yellow
        Remove-Item $venvPath -Recurse -Force -ErrorAction SilentlyContinue
        & $python -m venv $venvPath
        if ($LASTEXITCODE -ne 0) { throw "虚拟环境重建失败" }
        & $venvPy -m pip --version 2>$null | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "仍无 pip，尝试 get-pip.py ..." -ForegroundColor Yellow
        $getPip = Join-Path $env:TEMP "get-pip.py"
        foreach ($url in @(
            "https://mirrors.aliyun.com/pypi/get-pip.py",
            "https://bootstrap.pypa.io/get-pip.py"
        )) {
            try {
                Invoke-WebRequest -Uri $url -OutFile $getPip -TimeoutSec 60
                if (Test-Path $getPip) { break }
            } catch {}
        }
        if (-not (Test-Path $getPip)) { throw "无法下载 get-pip.py" }
        & $venvPy $getPip
        if ($LASTEXITCODE -ne 0) { throw "get-pip 安装失败" }
        & $venvPy -m pip --version 2>$null | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "无法为虚拟环境安装 pip" }
    Write-Host "pip 已修复。" -ForegroundColor Green
}

# ---- 3. 安装依赖 -------------------------------------------------------------
# pip 国内镜像按顺序回退：清华 TUNA → 阿里云 → 腾讯云 → 官方 PyPI。
$pipMirrors = @(
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://mirrors.cloud.tencent.com/pypi/simple",
    "https://pypi.org/simple"
)

function Invoke-PipMirrored {
    <#
    用镜像链逐个尝试 pip install；成功返回 $true，全部失败返回 $false。
    .PARAMETER Packages
        要安装的包参数数组（例如 @("-r", "requirements.txt")）。
    .PARAMETER UpgradePip
        只更新 pip 本体（忽略 Packages）。
    #>
    param([string[]]$Packages = @(), [switch]$UpgradePip)
    foreach ($mirror in $pipMirrors) {
        Write-Host "  尝试镜像: $mirror"
        if ($UpgradePip) {
            & $venvPy -m pip install --disable-pip-version-check `
                --upgrade pip -i $mirror --timeout 45 --retries 2
        } else {
            & $venvPy -m pip install --disable-pip-version-check `
                -i $mirror --timeout 45 --retries 2 @Packages
        }
        if ($LASTEXITCODE -eq 0) { return $true }
    }
    return $false
}

Write-Host "正在更新 pip ..." -ForegroundColor Yellow
if (-not (Invoke-PipMirrored -UpgradePip)) { throw "pip 更新失败" }
Write-Host "正在安装依赖 (numpy, Pillow, OpenCV, pywin32) ..." -ForegroundColor Yellow
if (-not (Invoke-PipMirrored @("-r", "requirements.txt"))) {
    throw "pip 依赖安装失败"
}

# ---- 4. 生成启动器 ------------------------------------------------------------
# 启动器内容必须为纯 ASCII：cmd 会用系统代码页解码 .bat，中文会乱码并破坏语法。
# BAT 只调用隐藏的 Windows Script Host。VBS 直接用 runas 启动 pythonw，
# 避免用 net session 判断权限失败后递归重启 BAT、造成命令行窗口反复闪烁。
$bat = @"
@echo off
cd /d "%~dp0"
if not exist "$venvName\Scripts\pythonw.exe" (
    echo Virtual environment missing. Run the setup first.
    pause
    exit /b 1
)
wscript.exe //nologo "%~dp0launch_assistant.vbs" %*
"@
$vbs = @'
Option Explicit

Dim shellApp, files, root, pythonwPath, exePath, assistantPath, arguments, item, statusPath
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = root & "\__VENV_DIR__\Scripts\pythonw.exe"
' Launch through a renamed interpreter so the running process is
' "TodoHelper.exe" (and the game sees that name) instead of "pythonw.exe".
' The copy is created on first use and self-heals after an overlay update.
exePath = root & "\__VENV_DIR__\Scripts\TodoHelper.exe"
On Error Resume Next
If Not files.FileExists(exePath) Then files.CopyFile pythonwPath, exePath, True
If Not files.FileExists(exePath) Then exePath = pythonwPath
On Error GoTo 0
' The probe records any exception that occurs before the normal application
' logging is available.  pythonw deliberately has no console, so without it
' a failed UAC launch looks like the BAT did nothing.
assistantPath = root & "\startup_probe.py"
statusPath = root & "\assistant-launch-status.log"
arguments = QuoteArgument(assistantPath)
For Each item In WScript.Arguments
    arguments = arguments & " " & QuoteArgument(CStr(item))
Next

Set shellApp = CreateObject("Shell.Application")
' Use the same integrity level as an elevated game. Without this, Windows
' will not deliver global low-level keyboard hooks for Ctrl hotkeys from the
' game window. Error handling below makes a declined/blocked UAC request
' visible instead of silently doing nothing.
WriteStatus "Launcher requested a hidden Python start."
On Error Resume Next
shellApp.ShellExecute exePath, arguments, root, "runas", 0
If Err.Number <> 0 Then
    WriteStatus "Windows could not start the assistant: " & Err.Description
    MsgBox "TodoHelper could not start. Open assistant-launch-status.log in this folder.", 16, "TodoHelper"
End If
On Error GoTo 0

Function QuoteArgument(value)
    QuoteArgument = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function

Sub WriteStatus(message)
    Dim logFile
    Set logFile = files.OpenTextFile(statusPath, 8, True, 0)
    logFile.WriteLine Now & " " & message
    logFile.Close
End Sub
'@
$vbs = $vbs.Replace("__VENV_DIR__", $venvName)
Set-Content -Path "start_assistant.bat" -Value $bat -Encoding ASCII
Set-Content -Path (Join-Path $root "启动助手.bat") -Value $bat -Encoding ASCII
Set-Content -Path (Join-Path $root "launch_assistant.vbs") -Value $vbs -Encoding ASCII
Write-Host "启动器已生成: start_assistant.bat / 启动助手.bat（隐藏命令行）" -ForegroundColor Green

# ---- 5. YOLO 怪物检测依赖（暂时停用；恢复步骤见 README.md） ------------------
Write-Host "已跳过 YOLO 怪物检测依赖（当前模型识别率不足）。" -ForegroundColor DarkYellow

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  安装完成。" -ForegroundColor Cyan
Write-Host "  双击 启动助手.bat（或 start_assistant.bat）即可开始。" -ForegroundColor Cyan
Write-Host "  首次启动会弹出 UAC 管理员权限确认，请点击“是”。" -ForegroundColor Yellow
Write-Host "  注意：游戏也必须以管理员权限运行，否则按键无法注入。" -ForegroundColor Yellow
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
