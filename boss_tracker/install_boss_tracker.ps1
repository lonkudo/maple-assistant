<#
One-click BOSS Tracker setup. Called by 安装.bat; ordinary users should not need
to run this file directly.
#>
param([switch]$NoLaunch)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"

function Find-Python310 {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $candidate = (& py -3.10 -c "import sys; print(sys.executable)" 2>$null)
            if ($candidate -and (Test-Path $candidate)) { return $candidate }
        } catch { }
    }
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:ProgramFiles\Python310\python.exe",
        "C:\Python310\python.exe"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Install-Python310 {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Python 3.10 was not found. Installing it with winget..." -ForegroundColor Yellow
        & winget install --id Python.Python.3.10 -e --silent `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            $installed = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
            if (Test-Path $installed) { return $installed }
        }
    }

    Write-Host "Downloading the official Python 3.10 installer..." -ForegroundColor Yellow
    $installer = Join-Path $env:TEMP "python-3.10.11-amd64.exe"
    Invoke-WebRequest `
        -Uri "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe" `
        -OutFile $installer
    $process = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList `
        "/quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_launcher=1 SimpleInstall=1"
    if ($process.ExitCode -ne 0) { throw "Python installer failed: $($process.ExitCode)" }
    $installed = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
    if (-not (Test-Path $installed)) { throw "Python 3.10 was installed but not found." }
    return $installed
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  BOSS Tracker setup" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

$python = Find-Python310
if (-not $python) { $python = Install-Python310 }
Write-Host "Using Python: $python" -ForegroundColor Green
& $python -c "import sys; assert sys.version_info[:2] == (3, 10); print(sys.version)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 check failed." }

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating the local environment..." -ForegroundColor Yellow
    & $python -m venv (Join-Path $root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv." }
}

Write-Host "Installing required packages from the Tsinghua mirror..." -ForegroundColor Yellow
& $venvPython -m pip install --disable-pip-version-check --upgrade pip -i $mirror
if ($LASTEXITCODE -ne 0) { throw "Could not update pip." }
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "requirements.txt") -i $mirror
if ($LASTEXITCODE -ne 0) { throw "Could not install required packages." }

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next time, double-click 启动BOSS追踪.bat." -ForegroundColor Green
if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $root "启动BOSS追踪.bat")
}
