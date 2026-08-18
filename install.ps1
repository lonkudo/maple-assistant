<#
.SYNOPSIS
    Maple Assistant bootstrap installer.

.DESCRIPTION
    Sets up everything needed to run the Maple Assistant on a fresh Windows
    machine:
      1. Finds a Python 3.10-3.12 interpreter (your environment), or installs
         one automatically (winget, then python.org fallback).
      2. Creates a local virtual environment (.venv).
      3. Installs the requirements.
      4. Creates the launchers (start_assistant.bat / 启动助手.bat).

    Optional: pass -Yolo to also create the YOLO detection environment
    (yolo-detection\venv313, downloads PyTorch - large, several GB).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Yolo
    powershell -ExecutionPolicy Bypass -File install.ps1 -Python C:\Python312\python.exe
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
Write-Host "  Maple Assistant installer" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

function Find-Python {
    <#
    Return the path of a usable Python 3.10-3.12, or $null.
    #>
    if ($Python) {
        if (-not (Test-Path $Python)) { throw "Python not found at: $Python" }
        return (Resolve-Path $Python).Path
    }
    # 1) The py launcher (per-user installs of Python 3.10-3.12).
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($ver in "3.12", "3.11", "3.10") {
            $exe = (& py -$ver -c "import sys;print(sys.executable)" 2>$null)
            if ($exe -and (Test-Path $exe)) { return $exe }
        }
    }
    # 2) python on PATH (only 3.10-3.12, never the WindowsApps stub).
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source -notmatch "WindowsApps") {
        $ver = (& $pythonCmd.Source -c "import sys;print('{0}.{1}'.format(*sys.version_info[:2]))" 2>$null)
        if ($ver -match "^(3\.(10|11|12))$") { return $pythonCmd.Source }
    }
    # 3) Common install locations.
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
    Install Python 3.12 for the current user. Tries winget first, then the
    python.org silent installer. Returns the installed python.exe path.
    #>
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Python not found - installing Python 3.12 via winget..." -ForegroundColor Yellow
        & winget install --id Python.Python.3.12 -e --silent `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            $exe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
            if (Test-Path $exe) { return $exe }
        }
    }
    Write-Host "Downloading Python 3.12 from python.org..." -ForegroundColor Yellow
    $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    $installer = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
    Invoke-WebRequest -Uri $url -OutFile $installer
    $quietArgs = "/quiet InstallAllUsers=0 PrependPath=0 Include_test=0 " +
        "Include_launcher=1 AssociateFiles=0 Shortcuts=0 SimpleInstall=1"
    $process = Start-Process -FilePath $installer -ArgumentList $quietArgs -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Python installer exited with code $($process.ExitCode)"
    }
    $exe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-Path $exe)) {
        throw "Python installed but python.exe not found; install Python 3.10-3.12 and re-run"
    }
    return $exe
}

# ---- 1. Python -------------------------------------------------------------
$python = Find-Python
if (-not $python) { $python = Install-Python }
Write-Host "Using Python: $python" -ForegroundColor Green
& $python -c "import sys; print('  version:', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw "Python check failed" }

# ---- 2. Virtual environment ------------------------------------------------
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating virtual environment .venv ..." -ForegroundColor Yellow
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
Write-Host "Virtual environment ready."

# ---- 3. Requirements --------------------------------------------------------
Write-Host "Installing requirements (numpy, Pillow, OpenCV, pywin32) ..." -ForegroundColor Yellow
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ---- 4. Launchers -----------------------------------------------------------
$bat = @"
@echo off
rem Maple Assistant launcher - runs the assistant with its virtual env.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment missing. Run install.ps1 first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" assistant.py %*
if errorlevel 1 pause
"@
Set-Content -Path "start_assistant.bat" -Value $bat -Encoding ASCII
Set-Content -Path (Join-Path $root "启动助手.bat") -Value $bat -Encoding Default
Write-Host "Launchers created: start_assistant.bat / 启动助手.bat" -ForegroundColor Green

# ---- 5. Optional YOLO environment -------------------------------------------
if ($Yolo) {
    Write-Host ""
    Write-Host "Setting up the YOLO detection environment (torch - this downloads" -ForegroundColor Yellow
    Write-Host "several GB and can take a long time) ..." -ForegroundColor Yellow
    $yoloVenv = Join-Path $root "yolo-detection\venv313\Scripts\python.exe"
    if (-not (Test-Path $yoloVenv)) {
        & $python -m venv "yolo-detection\venv313"
        if ($LASTEXITCODE -ne 0) { throw "yolo venv creation failed" }
    }
    & $yoloVenv -m pip install --upgrade pip
    & $yoloVenv -m pip install -r "yolo-detection\requirements.txt"
    if ($LASTEXITCODE -ne 0) { throw "yolo pip install failed" }
    Write-Host "YOLO environment ready (yolo-detection\venv313)." -ForegroundColor Green
    Write-Host "Put your trained model at yolo-detection\weights\best.pt" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Installation complete." -ForegroundColor Cyan
Write-Host "  Run 启动助手.bat (or start_assistant.bat) to start." -ForegroundColor Cyan
if (-not $Yolo) {
    Write-Host "  For YOLO mob detection, re-run with: install.ps1 -Yolo" -ForegroundColor Cyan
}
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
