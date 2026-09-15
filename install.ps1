<#
.SYNOPSIS
    Maple 助手 一键安装脚本（由 安装.bat 调用，用户无需手动运行）。

.DESCRIPTION
    在一台全新的 Windows 电脑上自动完成环境搭建：
      1. 查找本机已有的 Python 3.10-3.12（优先 3.10），
         找不到时自动下载并安装 Python 3.10（先尝试 winget，失败则从
         python.org 静默安装）。
      2. 创建本地虚拟环境（发布包会使用独立的 .venv-cpu 或 .venv-cuda）。
      3. 安装基础依赖库（优先阿里云镜像，失败自动回退官方 PyPI）。
      4. 暂不安装 YOLO 怪物检测依赖（模型重新训练后可按 README 恢复）。
      5. 生成启动器（start_assistant.bat / 启动助手.bat）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Python C:\Python312\python.exe
#>
param(
    [string]$Python = "",
    [ValidateSet("auto", "cpu", "cuda")]
    [string]$TrackerRuntime = "auto"
)

$ErrorActionPreference = "Stop"
# 自动过测谎 已移除：测谎由远端 RoiTrack 服务完成（测试api / api_lie_video.py）。
# 本地 Cutie/torch 追踪组件、离线权重与 vc_redist 不再随包分发，也不再安装；
# 下面的旧安装步骤由这个开关关闭（安装环境只剩助手本身的依赖）。
$InstallLocalTracker = $false

# PowerShell 7.3+ 会把外部命令的 stderr 当作终止错误（配合上面的 Stop）；
# 关闭该行为，让 "py: no such version" 之类的探测自然失败并尝试下一个候选，
# 而不是直接中断安装。
$PSNativeCommandUseErrorActionPreference = $false
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Release packages carry this tiny manifest.  It makes CPU and CUDA releases
# physically independent: each creates and launches only its own venv, even
# if both packages are unpacked side-by-side.
$variantManifest = Join-Path $root "release_variant.json"
if ($TrackerRuntime -eq "auto" -and (Test-Path $variantManifest)) {
    try {
        $manifest = Get-Content -LiteralPath $variantManifest -Raw |
            ConvertFrom-Json
        $declared = [string]$manifest.tracker_runtime
        if ($declared -in @("cpu", "cuda")) { $TrackerRuntime = $declared }
    } catch {
        Write-Warning "无法读取 release_variant.json；将使用自动设备安装。"
    }
}
$venvName = switch ($TrackerRuntime) {
    "cpu" { ".venv-cpu" }
    "cuda" { ".venv-cuda" }
    default { ".venv" }
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Maple 助手 安装程序" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  追踪运行环境: $($TrackerRuntime.ToUpper())" -ForegroundColor Cyan
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

# ---- 2.1 确保 VC++ 运行库（torch 的 c10.dll 依赖）----------------------------
# torch 需要 vcruntime140 / vcruntime140_1 / msvcp140。缺失时 c10.dll 会
# 报 WinError 1114（DLL 初始化失败）。优先安装随包携带的 vc_redist（离线），
# 缺失时从微软官方下载。安装.bat 已提权，静默安装不会弹 UAC。
function Test-VcRedistInstalled {
    # vc_redist 2015-2022 会安装的全部关键 DLL；缺任何一个 torch 都可能
    # 报 WinError 1114（c10.dll 初始化失败）。
    $required = @(
        "$env:SystemRoot\System32\vcruntime140.dll",
        "$env:SystemRoot\System32\vcruntime140_1.dll",
        "$env:SystemRoot\System32\msvcp140.dll",
        "$env:SystemRoot\System32\msvcp140_1.dll",
        "$env:SystemRoot\System32\msvcp140_2.dll",
        "$env:SystemRoot\System32\concrt140.dll"
    )
    foreach ($path in $required) {
        if (-not (Test-Path $path)) { return $false }
    }
    return $true
}
if ($InstallLocalTracker -and -not (Test-VcRedistInstalled)) {
    Write-Host "缺少 VC++ 运行库（torch 依赖），正在安装 ..." -ForegroundColor Yellow
    $isAdmin = ([Security.Principal.WindowsPrincipal]`
        [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(`
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Write-Warning "当前不是管理员权限；请通过 安装.bat（会请求 UAC）运行本脚本。"
    }
    $redistLocal = Join-Path $root "target_tracker\offline_bundle\installers\vc_redist.x64.exe"
    $redistPath = $redistLocal
    if (-not (Test-Path $redistLocal)) {
        Write-Host "未找到随包 vc_redist，正在从微软官方下载 ..." -ForegroundColor Yellow
        $redistPath = Join-Path $env:TEMP "vc_redist.x64.exe"
        try {
            Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" `
                -OutFile $redistPath -TimeoutSec 120
        } catch {
            Write-Warning "VC 运行库下载失败：$($_.Exception.Message)"
        }
    }
    if (Test-Path $redistPath) {
        $proc = Start-Process -FilePath $redistPath `
            -ArgumentList "/install", "/quiet", "/norestart" `
            -Wait -PassThru
        # 0/3010=成功(3010需重启), 1638/1641=已有更新版本（同样可用）
        $vcOkCodes = 0, 3010, 1638, 1641
        if ($vcOkCodes -notcontains $proc.ExitCode) {
            Write-Warning "VC++ 运行库安装退出码：$($proc.ExitCode)"
        }
    }
    if (-not (Test-VcRedistInstalled)) {
        Write-Warning "VC++ 运行库仍未就绪；自动过测谎 的 torch 可能无法加载。"
        Write-Warning "请以管理员身份重新运行 安装.bat（允许 UAC 弹窗）后重试。"
    } else {
        Write-Host "VC++ 运行库已就绪。" -ForegroundColor Green
    }
} elseif ($InstallLocalTracker) {
    Write-Host "VC++ 运行库检查通过（已安装）。" -ForegroundColor Green
}

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
# PyTorch(CPU) wheel 另有独立镜像链（上海交大 SJTU → 官方），见下方函数。
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

function Invoke-NativeConsole {
    <#
    Run a native command attached to the REAL console instead of through a
    PowerShell pipe.  PowerShell always pipes native output, so pip thinks
    stderr is not a terminal and silently disables its animated progress
    bar ("下载进度见下方 pip 输出" then nothing).  Start-Process without
    redirection lets the child inherit the console: pip then draws its
    per-file progress live in the installer window.  Returns the exit code.
    #>
    param([string]$FilePath, [string[]]$ArgumentList)
    $process = Start-Process -FilePath $FilePath `
        -ArgumentList $ArgumentList -NoNewWindow -Wait -PassThru
    return $process.ExitCode
}

function Install-TorchCpu {
    <#
    CPU 版 PyTorch：交大 SJTU 镜像（国内快）优先，官方 download.pytorch.org 兜底；
    依赖源同样走 $pipMirrors 回退链。
    -ForceReinstall 强制重装（修复损坏安装）。
    -Legacy 安装 Windows 10 兼容组合（torch 2.2.2 / torchvision 0.17.2）：
    torch 2.13+ 的新 wheel 在 Windows 10（build<22000）上加载 c10.dll 会崩
    （access violation / WinError 1114），必须用旧版。
    #>
    param([switch]$ForceReinstall, [switch]$Legacy)
    # Keep the normal CPU pair unpinned so the same official index resolves
    # mutually compatible torch/torchvision wheels.  The former fixed pair
    # combined torch 2.13 with torchvision 0.28, which pip rejects.
    $torchSpec = if ($Legacy) { "torch==2.2.2+cpu" } else { "torch" }
    $torchvisionSpec = if ($Legacy) { "torchvision==0.17.2+cpu" } else { "torchvision" }
    $torchIndexes = @(
        "https://mirror.sjtu.edu.cn/pytorch-wheels/cpu/",
        "https://download.pytorch.org/whl/cpu/"
    )
    foreach ($index in $torchIndexes) {
        foreach ($mirror in $pipMirrors) {
            Write-Host "  尝试 PyTorch 源: $index (依赖源: $mirror)"
            Write-Host "  torch wheel 约 200MB - 下载进度见下方 pip 输出 ..."
            # Run pip attached to the real console so its animated per-file
            # progress bar is actually drawn (PowerShell pipes would make pip
            # think stderr is not a terminal and hide the progress).  Success
            # is judged from the process exit code, not $LASTEXITCODE.
            $pipArgs = @(
                "-m", "pip", "install", "--disable-pip-version-check",
                $torchSpec, $torchvisionSpec,
                "--index-url", $index, "--extra-index-url", $mirror,
                "--timeout", "90", "--retries", "2"
            )
            if ($ForceReinstall) { $pipArgs += "--force-reinstall" }
            $pipExit = Invoke-NativeConsole -FilePath $venvPy -ArgumentList $pipArgs
            if ($pipExit -eq 0) { return $true }
        }
    }
    Write-Warning "所有 CPU PyTorch 下载源都未能完成安装。"
    return $false
}

function Test-NvidiaCudaDriver {
    """True only when Windows can see an NVIDIA driver and at least one GPU."""

    $candidates = @()
    $command = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $systemCopy = Join-Path $env:SystemRoot "System32\nvidia-smi.exe"
    if (Test-Path $systemCopy) { $candidates += $systemCopy }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        try {
            $output = & $candidate -L 2>$null
            if ($LASTEXITCODE -eq 0 -and $output) { return $true }
        } catch {}
    }
    return $false
}

function Get-NvidiaGpuInfo {
    <#
    Read the target machine's NVIDIA card: name, compute capability, driver
    version and the CUDA version that driver supports (nvidia-smi prints it as
    "CUDA Version: 13.1").  Returns $null when no card/driver/nvidia-smi is
    available - a machine without an NVIDIA GPU has nothing to install here.

    This is what lets the installer pick ONE correct CUDA wheel line up front
    instead of downloading (2.7GB each) and retrying versions until one works.
    #>
    param()
    $smi = $null
    $command = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($command) { $smi = $command.Source }
    if (-not $smi) {
        foreach ($candidate in @(
            (Join-Path $env:SystemRoot "System32\nvidia-smi.exe"),
            (Join-Path ${env:ProgramFiles} "NVIDIA Corporation\NVSMI\nvidia-smi.exe")
        )) {
            if ($candidate -and (Test-Path -LiteralPath $candidate)) { $smi = $candidate; break }
        }
    }
    if (-not $smi) { return $null }

    $info = [ordered]@{ Name = ""; Capability = $null; Driver = ""; CudaCeiling = $null }
    try {
        $query = & $smi --query-gpu=name,compute_cap,driver_version `
            --format=csv,noheader 2>$null | Select-Object -First 1
        if ($query) {
            $parts = $query -split ','
            if ($parts.Count -ge 1) { $info.Name = $parts[0].Trim() }
            if ($parts.Count -ge 2 -and $parts[1] -match '(\d+)\.(\d+)') {
                $info.Capability = [double]::Parse(
                    "$($matches[1]).$($matches[2])",
                    [System.Globalization.CultureInfo]::InvariantCulture)
            }
            if ($parts.Count -ge 3) { $info.Driver = $parts[2].Trim() }
        }
    } catch {
        # Fall through to the header parse below.
    }
    try {
        $header = & $smi 2>$null | Select-String -Pattern 'CUDA Version:\s*([0-9]+\.[0-9]+)' |
            Select-Object -First 1
        if ($header -and $header.Matches.Count -gt 0) {
            $info.CudaCeiling = [double]::Parse(
                $header.Matches[0].Groups[1].Value,
                [System.Globalization.CultureInfo]::InvariantCulture)
        }
    } catch {
        # A driver that hides its version only costs us the "newest" pick.
    }
    if (-not $info.Name -and $null -eq $info.Capability -and $null -eq $info.CudaCeiling) {
        return $null
    }
    return $info
}

function Get-CudaWheelCandidates {
    <#
    Every CUDA wheel line this machine could use, newest first, decided before
    anything is downloaded.

    Coverage per line (measured on an RTX 5070 Ti where possible):
      cu118 / cu124 / cu126 : sm_50 .. sm_90 (Maxwell .. Hopper)
      cu128                 : sm_75 .. sm_120 (verified locally)
      cu129 / cu130         : sm_75 .. sm_120 and newer

    Two filters, both answered by this machine's own hardware:
      compute capability -> which lines can run this card at all
      driver CUDA version -> which lines this driver supports
    The FIRST entry is the newest line that passes both, so an RTX 5070 Ti on a
    driver advertising CUDA 13.1 picks cu130, while an older card on an older
    driver still lands on cu118.  The remaining entries exist so a line that a
    mirror does not carry is skipped by a millisecond HEAD request instead of by
    downloading a 2.7GB wheel and retrying.
    #>
    param(
        [double]$Capability = 0,
        [double]$CudaCeiling = 0
    )
    $table = @(
        [pscustomobject]@{ Line = "cu130"; Min = 7.5; Max = 99.0; Cuda = 13.0 },
        [pscustomobject]@{ Line = "cu129"; Min = 7.5; Max = 99.0; Cuda = 12.9 },
        [pscustomobject]@{ Line = "cu128"; Min = 7.5; Max = 99.0; Cuda = 12.8 },
        [pscustomobject]@{ Line = "cu126"; Min = 5.0; Max = 9.0;  Cuda = 12.6 },
        [pscustomobject]@{ Line = "cu124"; Min = 5.0; Max = 9.0;  Cuda = 12.4 },
        [pscustomobject]@{ Line = "cu118"; Min = 5.0; Max = 9.0;  Cuda = 11.8 }
    )
    $lines = @()
    foreach ($row in $table) {
        if ($Capability -gt 0 -and ($Capability -lt $row.Min -or $Capability -gt $row.Max)) {
            continue
        }
        if ($CudaCeiling -gt 0 -and $CudaCeiling -lt $row.Cuda) {
            continue
        }
        $lines += $row.Line
    }
    if ($lines.Count -eq 0) {
        # Unknown card and unknown driver: the line with the widest coverage.
        $lines = @("cu118")
    }
    return $lines
}

function Select-CudaWheelLine {
    <#
    The ONE CUDA wheel line to install: the newest line this machine can run.
    #>
    param(
        [double]$Capability = 0,
        [double]$CudaCeiling = 0
    )
    # Wrap in @(): a single candidate comes back as a bare string, and [0] on a
    # string returns its first character instead of the line name.
    $lines = @(Get-CudaWheelCandidates -Capability $Capability -CudaCeiling $CudaCeiling)
    if ($lines.Count -eq 0) { return "cu118" }
    return $lines[0]
}

function Test-CudaIndexAvailable {
    <#
    Cheap existence check for one CUDA wheel index URL, so a line that a mirror
    does not carry is skipped in milliseconds instead of after a 2.7GB download
    attempt.

    A plain GET, not a HEAD: some mirror front ends answer 405 to HEAD, which
    would look exactly like "this version does not exist" and silently downgrade
    a capable machine.  The index page itself is only a few KB.

    Returns $true when the check cannot run at all (offline probe): the install
    itself is then the only authority.
    #>
    param([Parameter(Mandatory = $true)][string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url `
            -TimeoutSec 20 -UseBasicParsing -ErrorAction Stop
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400)
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = $_.Exception.Response.StatusCode.value__ }
        if ($status) { return $false }
        return $true          # network/probe problem, not a missing line
    }
}

function Invoke-VenvPythonFile {
    <#
    Run a Python snippet through one interpreter from a temporary .py file and
    return @{ Output = <lines>; Code = <exit code> }.

    The snippet must go through a FILE, never through `python -c`: Windows
    PowerShell 5.1 drops embedded double quotes when it hands a string argument
    to a native program, so `python -c 'print("CUDA_OK")'` actually arrives as
    `print(CUDA_OK)` and raises NameError instead of printing a result.  A
    check written that way reports a healthy install as broken.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$VenvPy,
        [Parameter(Mandatory = $true)][string]$Code
    )
    if (-not (Test-Path -LiteralPath $VenvPy)) {
        Write-Warning "  找不到 Python 解释器: $VenvPy"
        return @{ Output = @(); Code = 1 }
    }
    $temp = [System.IO.Path]::Combine(
        [System.IO.Path]::GetTempPath(),
        ("maple_tracker_probe_{0}.py" -f [guid]::NewGuid().ToString("N")))
    try {
        # UTF-8 without BOM: Python 3 already reads source as UTF-8, and a BOM
        # would only confuse anything that inspects the file later.
        [System.IO.File]::WriteAllText($temp, $Code, (New-Object System.Text.UTF8Encoding($false)))
        $output = & $VenvPy $temp 2>&1
        $exitCode = $LASTEXITCODE
        return @{ Output = $output; Code = $exitCode }
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}

function Get-VenvPythonTag {
    <#
    The wheel tag of the interpreter that will run the tracker, e.g. "cp310".
    Returned empty when it cannot be determined (the wheel checks are then
    skipped rather than guessed).
    #>
    param([Parameter(Mandatory = $true)][string]$VenvPy)
    $code = @'
import sys
print("cp%d%d" % sys.version_info[:2])
'@
    $result = Invoke-VenvPythonFile -VenvPy $VenvPy -Code $code
    if ($result.Code -ne 0) { return "" }
    $line = @($result.Output | Where-Object { "$_".Trim() -match '^cp3\d+$' })[0]
    if (-not $line) { return "" }
    return "$line".Trim()
}

function Test-TorchWheelListing {
    <#
    Does one torch/ index listing contain a Windows torch wheel for this Python
    tag?  $null when the tag is unknown or the listing is empty, so an
    unreadable page can never turn into a wrongly skipped version.

    Matches names like torch-2.9.0+cu130-cp310-cp310-win_amd64.whl, and
    deliberately not torchvision-... nor a manylinux torch wheel.
    #>
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content,
        [string]$PythonTag = ""
    )
    if (-not $PythonTag -or -not $Content) { return $null }
    $pattern = 'torch-[0-9][^"''<>]*' + [regex]::Escape($PythonTag) + '-[^"''<>]*win_amd64\.whl'
    return [regex]::IsMatch($Content, $pattern)
}

function Test-CudaLineHasTorchWheel {
    <#
    Does this CUDA wheel index really carry a Windows torch wheel for the
    interpreter that will use it?

    The index root answering a HEAD is not enough: a line can be published and
    still have no build for this Python version (or no Windows build at all),
    which pip would only discover after starting the download.  Reading the
    line's torch/ listing settles it beforehand.

    $true  = a matching win_amd64 wheel is listed
    $false = the listing was read and contains none
    $null  = the listing could not be read, so the install stays the authority
    #>
    param(
        [Parameter(Mandatory = $true)][string]$IndexUrl,
        [string]$PythonTag = ""
    )
    if (-not $PythonTag) { return $null }
    try {
        $page = Invoke-WebRequest -Uri ($IndexUrl.TrimEnd('/') + "/torch/") `
            -TimeoutSec 30 -UseBasicParsing -ErrorAction Stop
    } catch {
        return $null
    }
    $content = $page.Content
    if ($content -is [byte[]]) { $content = [System.Text.Encoding]::UTF8.GetString($content) }
    return Test-TorchWheelListing -Content ([string]$content) -PythonTag $PythonTag
}

function Test-InstalledCudaSupport {
    <#
    Verify the freshly installed torch can RUN kernels on this machine's card.
    0 = usable, 3 = CUDA unusable (the GUI falls back to CPU), 5 = this wheel
    does not carry the card's compute capability.
    #>
    param([Parameter(Mandatory = $true)][string]$VenvPy)
    $script = @'
import torch
if not torch.cuda.is_available():
    print("CUDA_NOT_AVAILABLE")
    raise SystemExit(3)
try:
    cap = torch.cuda.get_device_capability(0)
    arch = torch.cuda.get_arch_list()
    name = torch.cuda.get_device_name(0)
except Exception as exc:
    print("CUDA_PROBE_FAILED", exc)
    raise SystemExit(3)
needed = "sm_%d%d" % (cap[0], cap[1])
if arch and needed not in arch:
    print("CUDA_UNSUPPORTED", name, needed, torch.__version__, arch)
    raise SystemExit(5)
print("CUDA_OK", name, needed, torch.__version__)
'@
    $result = Invoke-VenvPythonFile -VenvPy $VenvPy -Code $script
    $result.Output | ForEach-Object { Write-Host "  $_" }
    return $result.Code
}

function Install-TorchCuda {
    <#
    Install the CUDA-enabled PyTorch wheel.  These wheels bundle the CUDA
    runtime used by PyTorch, so a separate CUDA Toolkit is NOT required for
    Maple Assistant.  An NVIDIA GPU and a working NVIDIA display driver are
    still required; verify that with torch.cuda.is_available() afterwards.

    The CUDA line is decided BEFORE anything is downloaded, from this machine
    only: nvidia-smi reports the card name, its compute capability and the CUDA
    version the installed driver supports (Get-NvidiaGpuInfo), and
    Select-CudaWheelLine picks the newest line that both carries that compute
    capability and is not newer than the driver (e.g. sm_120 + driver CUDA 13.1
    -> cu130; an old card on an old driver still lands on cu118).  Exactly one
    line is installed, so a wrong guess never costs a 2.7GB download and there
    is no install-then-retry loop; the mirror and download.pytorch.org are
    tried for that same line only.  The installed wheel is then verified once
    against the card's compute capability, and a wheel that cannot run there is
    reported honestly (tracking falls back to CPU) instead of being papered
    over with another download.  Normal dependencies retain the domestic mirror
    fallback chain.

    .PARAMETER Legacy
    Install the Windows 10 compatible pair (torch 2.2.2+cu118 /
    torchvision 0.17.2+cu118).  Newer CUDA wheels can fail to load their
    bundled DLLs (caffe2_nvrtc / c10, WinError 126/1114) on Windows 10
    builds below 22000, exactly like the CPU pair.
    #>
    param([switch]$Legacy)
    if ($Legacy) {
        # Windows 10 builds below 22000 need the old pair; newer CUDA wheels
        # fail to load their bundled DLLs there.  Nothing to choose.
        $lines = @("cu118")
        $torchSpec = "torch==2.2.2+cu118"
        $torchvisionSpec = "torchvision==0.17.2+cu118"
        Write-Host "  Windows 10 兼容模式: torch 2.2.2+cu118"
    } else {
        # Decide the CUDA wheel line BEFORE downloading anything (each wheel is
        # ~2.7GB, so "install one, see it fail, try the next" is not
        # acceptable).  The order comes from this machine's own hardware:
        #   compute capability -> which lines can run the card at all
        #   driver CUDA version -> which lines the driver supports
        $torchSpec = "torch"
        $torchvisionSpec = "torchvision"
        $gpu = Get-NvidiaGpuInfo
        if ($null -eq $gpu) {
            Write-Host "  未检测到 NVIDIA 显卡/驱动 -> 按最保守的 cu118 安装"
            $lines = @("cu118")
        } else {
            $capability = if ($null -ne $gpu.Capability) { $gpu.Capability } else { 0 }
            $ceiling = if ($null -ne $gpu.CudaCeiling) { $gpu.CudaCeiling } else { 0 }
            Write-Host ("  显卡: {0}  算力: {1}  驱动: {2}  驱动支持 CUDA: {3}" -f `
                $gpu.Name, $(if ($capability -gt 0) { $capability } else { "未知" }), `
                $(if ($gpu.Driver) { $gpu.Driver } else { "未知" }), `
                $(if ($ceiling -gt 0) { $ceiling } else { "未知" }))
            $lines = Get-CudaWheelCandidates -Capability $capability -CudaCeiling $ceiling
            Write-Host ("  本机可用 CUDA 版本（从新到旧）: {0}" -f ($lines -join ", "))
        }
    }

    # Pick the newest line that a source actually carries a usable wheel for.
    # Both checks are pre-download: a HEAD on the index and one listing of its
    # torch/ directory.  A failure here costs a millisecond, never a 2.7GB
    # download, and nothing is installed until one line has been chosen.
    $pythonTag = Get-VenvPythonTag -VenvPy $venvPy
    $line = $null
    $usableHosts = @()
    foreach ($candidateLine in $lines) {
        $hosts = @(
            "https://mirror.sjtu.edu.cn/pytorch-wheels/$candidateLine/",
            "https://download.pytorch.org/whl/$candidateLine/"
        )
        $lineHosts = @()
        foreach ($candidateHost in $hosts) {
            if (-not (Test-CudaIndexAvailable -Url $candidateHost)) {
                Write-Host "    $candidateHost -> 源上不存在" -ForegroundColor DarkGray
                continue
            }
            # Returns $null when the listing cannot be read, so an unreadable
            # page never turns into a wrongly skipped version.
            $hasWheel = Test-CudaLineHasTorchWheel -IndexUrl $candidateHost -PythonTag $pythonTag
            if ($hasWheel -eq $false) {
                Write-Host "    $candidateHost -> 没有 Python $pythonTag 的 Windows torch 轮子" -ForegroundColor DarkGray
                continue
            }
            $lineHosts += $candidateHost
        }
        if ($lineHosts.Count -gt 0) {
            $line = $candidateLine
            $usableHosts = $lineHosts
            break
        }
        Write-Host "    跳过 $candidateLine（源上无可用 torch 轮子）" -ForegroundColor Yellow
    }
    if (-not $line) {
        Write-Warning ("  所有候选 CUDA 版本在镜像与官方源上都不可用: {0}" -f ($lines -join ", "))
        return $false
    }
    if ($line -ne $lines[0]) {
        Write-Host "  选定 CUDA 版本: $line（比 $($lines[0]) 旧，但源上确认可用）" -ForegroundColor Yellow
    } else {
        Write-Host "  选定 CUDA 版本: $line" -ForegroundColor Green
    }

    foreach ($cudaIndex in $usableHosts) {
        Write-Host "  安装 CUDA PyTorch 源: $cudaIndex"
        # Run pip attached to the real console so its animated per-file
        # progress bar is actually drawn (PowerShell pipes would make pip
        # think stderr is not a terminal and hide the progress).  Long
        # timeout + many retries let a dropped connection resume instead of
        # failing the whole install.  Success is judged from the process
        # exit code, not $LASTEXITCODE.
        # Keep the CUDA index as the only package source here.  An extra
        # ordinary PyPI mirror may offer a numerically newer CPU torch wheel,
        # which pip would prefer and would silently make the CUDA tester
        # unavailable.
        $pipArgs = @(
            "-m", "pip", "install", "--disable-pip-version-check",
            "--upgrade", "--force-reinstall", $torchSpec, $torchvisionSpec,
            "--index-url", $cudaIndex, "--timeout", "300", "--retries", "5"
        )
        $pipExit = Invoke-NativeConsole -FilePath $venvPy -ArgumentList $pipArgs
        if ($pipExit -eq 0) {
            # Verify once: a wheel that cannot run on this card is reported
            # honestly instead of being papered over by another 2.7GB download.
            $probe = Test-InstalledCudaSupport -VenvPy $venvPy
            if ($probe -eq 0) { return $true }
            Write-Warning ("  已安装 {0} 的 torch 无法在本机显卡上运行；追踪将自动改用 CPU" -f $line)
            Write-Warning "  如需修复，可手动运行: .venv\Scripts\python.exe -m pip install --force-reinstall torch torchvision --index-url https://download.pytorch.org/whl/cu128"
            return $false
        }
        Write-Warning "  安装失败: $cudaIndex（尝试同一版本的备用源）"
    }
    return $false
}

function Install-Cutie {
    param([string]$WheelPath)
    if (-not (Test-Path -LiteralPath $WheelPath)) {
        Write-Warning "未找到随包 Cutie 安装文件: $WheelPath"
        return $false
    }
    # The bundled wheel is intentionally local/offline.  Force reinstall
    # prevents an interrupted earlier install from appearing successful.
    $pipOutput = & $venvPy -m pip install --disable-pip-version-check `
        --no-index --no-deps --force-reinstall $WheelPath 2>&1
    $pipExit = $LASTEXITCODE
    $pipOutput | ForEach-Object { Write-Host $_ }
    if ($pipExit -ne 0) { return $false }
    $importOutput = & $venvPy -c "import cutie; import cutie.inference.inference_core; print('Cutie OK', cutie.__file__)" 2>&1
    $importExit = $LASTEXITCODE
    $importOutput | ForEach-Object { Write-Host $_ }
    return ($importExit -eq 0)
}

Write-Host "正在更新 pip ..." -ForegroundColor Yellow
if (-not (Invoke-PipMirrored -UpgradePip)) { throw "pip 更新失败" }
Write-Host "正在安装依赖 (numpy, Pillow, OpenCV, pywin32) ..." -ForegroundColor Yellow
if (-not (Invoke-PipMirrored @("-r", "requirements.txt"))) {
    throw "pip 依赖安装失败"
}

# ---- 自动过测谎 追踪组件（已停用，见文件顶部的 $InstallLocalTracker）-----------
if ($InstallLocalTracker) {
# ---- 自动过测谎（渐隐方块追踪组件）---------------------------------------------
# 有 NVIDIA GPU + 驱动时优先装 CUDA PyTorch；否则自动回退 CPU。CUDA wheel 自带
# PyTorch 所需的运行时，不需要另装完整 CUDA Toolkit。安装失败只提示，不中断主安装。
$trackerBundle = Join-Path $root "target_tracker"
$cutieWheel = Join-Path $trackerBundle "offline_bundle\wheelhouse\cutie-1.0.0-py3-none-any.whl"
Write-Host "正在安装 自动过测谎 追踪组件 ..." -ForegroundColor Yellow
try {
    # Inside this block every failure is already handled by explicit
    # $LASTEXITCODE checks and throws.  Scoping ErrorActionPreference to
    # Continue stops PowerShell 5.1 from turning captured native stderr
    # (e.g. a torch import traceback) into a cryptic terminating error
    # whose message is just "Traceback (most recent call last):".
    $ErrorActionPreference = "Continue"
    # Windows 10 (build<22000) 上新 torch 2.13 无法加载 c10.dll → 直接装兼容旧版。
    $winBuildNumber = 0
    try {
        $winBuildNumber = [int]((Get-CimInstance Win32_OperatingSystem).BuildNumber)
    } catch {}
    $legacyTorch = $winBuildNumber -gt 0 -and $winBuildNumber -lt 22000
    if ($legacyTorch) {
        Write-Host "检测到 Windows 10（build $winBuildNumber）：新 torch 在 Win10 上" -ForegroundColor Yellow
        Write-Host "无法加载（c10/caffe2_nvrtc，WinError 1114/126）；CPU 与 CUDA 均改用" -ForegroundColor Yellow
        Write-Host "兼容版 torch 2.2.2 + numpy 1.26 ..." -ForegroundColor Yellow
    }
    $torchUsingCuda = $false
    $torchCheck = $null
    $nvidiaReady = Test-NvidiaCudaDriver
    $wantCuda = $TrackerRuntime -eq "cuda" -or (
        $TrackerRuntime -eq "auto" -and $nvidiaReady
    )
    if ($wantCuda -and -not $nvidiaReady) {
        if ($TrackerRuntime -eq "cuda") {
            throw "CUDA 发布包需要 NVIDIA GPU 和可用 NVIDIA 驱动；未检测到 nvidia-smi。"
        }
        Write-Warning "未检测到可用 NVIDIA 驱动；将安装 CPU PyTorch。"
    }
    if ($wantCuda -and $nvidiaReady) {
        Write-Host "正在安装 CUDA PyTorch（下载较大，请耐心等待）..." -ForegroundColor Yellow
        if (Install-TorchCuda -Legacy:$legacyTorch) {
            # torch 2.2.2 must never be imported while NumPy 2.x is installed
            # (its stderr compat warning can abort the PowerShell 5.1 flow).
            if ($legacyTorch) {
                if (-not (Invoke-PipMirrored @("numpy==1.26.4"))) {
                    throw "numpy 1.26 安装失败"
                }
            }
            $torchCheck = & $venvPy -c "import torch; assert torch.cuda.is_available(); print('OK', torch.__version__, 'CUDA', torch.version.cuda, torch.cuda.get_device_name(0))" 2>&1
            if ($LASTEXITCODE -eq 0) {
                $torchUsingCuda = $true
                Write-Host "CUDA PyTorch 验证通过。" -ForegroundColor Green
            } else {
                if ($TrackerRuntime -eq "cuda") {
                    Write-Warning "CUDA PyTorch 已下载但无法使用 GPU；CUDA 发布包不能回退 CPU。"
                } else {
                    Write-Warning "CUDA PyTorch 已下载但无法使用 GPU；将自动回退 CPU。"
                }
                Write-Warning ($torchCheck | Select-Object -Last 3)
            }
        } else {
            if ($TrackerRuntime -eq "cuda") {
                Write-Warning "CUDA PyTorch 下载失败；CUDA 发布包不能回退 CPU。"
            } else {
                Write-Warning "CUDA PyTorch 下载失败；将自动回退 CPU。"
            }
        }
    } elseif ($TrackerRuntime -eq "cpu") {
        Write-Host "CPU 发布包：安装 CPU PyTorch。" -ForegroundColor Yellow
    } elseif (-not $wantCuda) {
        Write-Host "未检测到可用 NVIDIA 驱动；安装 CPU PyTorch。" -ForegroundColor Yellow
    }
    if (-not $torchUsingCuda) {
        if ($TrackerRuntime -eq "cuda") {
            throw "CUDA 发布包无法建立 CUDA PyTorch 环境；不会改装 CPU 环境。" +
                "请检查网络后重新运行 安装.bat（CUDA torch wheel 约 2.7GB，" +
                "已优先使用国内 SJTU 镜像；若提示 WinError 32，通常是杀毒软件" +
                "锁定大文件，重试即可）。"
        }
        if (-not (Install-TorchCpu -Legacy:$legacyTorch)) { throw "PyTorch(CPU) 安装失败" }
        # Torch 2.2 on Windows 10 must never be imported while NumPy 2.x is
        # installed: it writes a compatibility warning to stderr. PowerShell
        # 5.1 can promote that warning to a fatal error. Downgrade first.
        if ($legacyTorch) {
            if (-not (Invoke-PipMirrored @("numpy==1.26.4"))) {
                throw "numpy 1.26 安装失败"
            }
        }
        $torchCheck = & $venvPy -c "import torch; print('OK', torch.__version__)" 2>&1
    }
    if (-not (Invoke-PipMirrored @(
        "hydra-core==1.3.2", "omegaconf==2.3.0",
        "antlr4-python3-runtime==4.9.3", "tqdm", "requests"
    ))) { throw "追踪运行依赖安装失败" }
    if (-not (Install-Cutie $cutieWheel)) {
        throw "Cutie 安装或导入验证失败"
    }
    $purelib = (& $venvPy -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    $weightDir = Join-Path $purelib "weights"
    New-Item -ItemType Directory -Force -Path $weightDir | Out-Null
    Get-ChildItem (Join-Path $trackerBundle "offline_bundle\weights\*.pth") `
        -ErrorAction SilentlyContinue | Copy-Item -Destination $weightDir -Force
    # 离线权重必须齐全：Cutie 主权重 + ResNet 骨干（pixel encoder=resnet50,
    # mask encoder=resnet18）。缺失时 torch.hub 会尝试联网下载，而发布包在
    # pythonw（无控制台流）下运行且可能离线，启动预热会直接崩溃。
    $requiredWeights = @(
        "cutie-base-mega.pth", "coco_lvis_h18_itermask.pth",
        "resnet50-19c8e357.pth", "resnet18-5c106cde.pth"
    )
    $missingWeights = @($requiredWeights | Where-Object {
        -not (Test-Path (Join-Path $weightDir $_))
    })
    if ($missingWeights.Count -gt 0) {
        throw "发布包缺少离线权重文件: $($missingWeights -join ', ')"
    }
    # 关键：装完必须验证 torch 真能加载（WinError 1114 只有运行时才暴露）。
    Write-Host "正在验证 torch 能否加载 ..." -ForegroundColor Yellow
    if ($null -eq $torchCheck) {
        $torchCheck = & $venvPy -c "import torch; print('OK', torch.__version__)" 2>&1
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "首次加载失败，尝试修复安装 VC++ 运行库后重试 ..." -ForegroundColor Yellow
        $redistLocal = Join-Path $root "target_tracker\offline_bundle\installers\vc_redist.x64.exe"
        if (Test-Path $redistLocal) {
            $proc = Start-Process -FilePath $redistLocal `
                -ArgumentList "/repair", "/quiet", "/norestart" `
                -Wait -PassThru
            Write-Host "VC++ 运行库修复退出码：$($proc.ExitCode)"
        } else {
            Write-Warning "未找到随包 vc_redist，跳过修复。"
        }
        $torchCheck = & $venvPy -c "import torch; print('OK', torch.__version__)" 2>&1
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "修复后仍失败，尝试强制重装 torch（约 200MB）..." -ForegroundColor Yellow
        if ($torchUsingCuda) {
            if (-not (Install-TorchCuda -Legacy:$legacyTorch)) {
                throw "CUDA torch 强制重装失败"
            }
            if ($legacyTorch) {
                if (-not (Invoke-PipMirrored @("numpy==1.26.4"))) {
                    throw "numpy 1.26 安装失败"
                }
            }
            $torchCheck = & $venvPy -c "import torch; assert torch.cuda.is_available(); print('OK', torch.__version__, 'CUDA', torch.version.cuda)" 2>&1
        } else {
            if (-not (Install-TorchCpu -ForceReinstall -Legacy:$legacyTorch)) {
                throw "torch 强制重装失败"
            }
            if ($legacyTorch) {
                if (-not (Invoke-PipMirrored @("numpy==1.26.4"))) {
                    throw "numpy 1.26 安装失败"
                }
            }
            $torchCheck = & $venvPy -c "import torch; print('OK', torch.__version__)" 2>&1
        }
    }
    if ($LASTEXITCODE -ne 0 -and -not $legacyTorch -and -not $torchUsingCuda) {
        # 最后兜底：新 torch 始终失败时尝试 Win10 兼容组合。
        Write-Host "新 torch 始终无法加载，改用 Windows 10 兼容组合 2.2.2 ..." -ForegroundColor Yellow
        $legacyTorch = $true
        if (Install-TorchCpu -Legacy) {
            if (Invoke-PipMirrored @("numpy==1.26.4")) {
                $torchCheck = & $venvPy -c "import torch; print('OK', torch.__version__)" 2>&1
            }
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "torch 加载失败：" -ForegroundColor Yellow
        Write-Host ($torchCheck | Select-Object -Last 4)
        Write-Host "请把以上错误发给开发者；同时在该机器命令行执行下面两条并" -ForegroundColor Yellow
        Write-Host "把输出一起发来：" -ForegroundColor Yellow
        Write-Host "  dir C:\Windows\System32\vcruntime140*.dll " -ForegroundColor Yellow
        Write-Host "  dir C:\Windows\System32\msvcp140*.dll" -ForegroundColor Yellow
        throw "torch 加载验证失败"
    }
    Write-Host "torch 验证通过：$torchCheck" -ForegroundColor Green
    Write-Host ("自动过测谎 追踪组件（{0}）安装完成。" -f `
        $(if ($torchUsingCuda) { "CUDA" } else { "CPU" })) -ForegroundColor Green
} catch {
    $trackerError = $_.Exception.Message
    if (-not $trackerError) { $trackerError = "安装命令返回失败状态；请查看上方 pip 输出。" }
    Write-Host "自动过测谎 组件安装失败：$trackerError" -ForegroundColor Yellow
    if ($TrackerRuntime -in @("cpu", "cuda")) {
        throw "$($TrackerRuntime.ToUpper()) 发布包安装失败：$trackerError"
    }
    Write-Host "可稍后重新运行安装脚本重试；不影响其他功能。" -ForegroundColor Yellow
}
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
' "todo_helper.exe" (and the game sees that name) instead of "pythonw.exe".
' The copy is created on first use and self-heals after an overlay update.
exePath = root & "\__VENV_DIR__\Scripts\todo_helper.exe"
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
    MsgBox "todo_helper could not start. Open assistant-launch-status.log in this folder.", 16, "todo_helper"
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
