<#
.SYNOPSIS
    Build a minimal, distributable Maple Assistant release folder.

.DESCRIPTION
    Copies only the files needed to run (no venvs, logs, tests, work data,
    git metadata).  The result can be zipped and given to another user, who
    then runs install.ps1 inside it to bootstrap Python + requirements.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build_release.ps1
    powershell -ExecutionPolicy Bypass -File build_release.ps1 -Zip
#>
param(
    [string]$OutDir = "release\MapleAssistant",
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $root $OutDir
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path $out -Force | Out-Null
Write-Host "Building release into: $out" -ForegroundColor Cyan

# --- root runtime files -------------------------------------------------------
$rootFiles = Get-ChildItem $root -File | Where-Object {
    $name = $_.Name
    ($_.Extension -in ".py", ".json", ".md", ".ps1", ".vbs", ".bat", ".txt") -and
    $name -notlike "test_*" -and $name -ne "auto_system.log"
}
foreach ($f in $rootFiles) { Copy-Item $f.FullName $out }

# --- yolo-detection -----------------------------------------------------------
$yoloOut = Join-Path $out "yolo-detection"
New-Item -ItemType Directory -Path $yoloOut -Force | Out-Null
Get-ChildItem (Join-Path $root "yolo-detection") -File | Where-Object {
    ($_.Extension -in ".py", ".yaml", ".txt", ".md") -and
    $_.Name -notin @(
        "elevated_result.txt", "work-check.txt", "live_test_decision.py"
    ) -and
    $_.Name -notlike "*.log"
} | ForEach-Object { Copy-Item $_.FullName $yoloOut }
# Trained model weights (best.pt ~6MB; best.onnx ~12MB optional).
$weightsIn = Join-Path $root "yolo-detection\weights"
$weightsOut = Join-Path $yoloOut "weights"
if (Test-Path $weightsIn) {
    New-Item -ItemType Directory -Path $weightsOut -Force | Out-Null
    Copy-Item (Join-Path $weightsIn "best.pt") $weightsOut -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $weightsIn "best.onnx") $weightsOut -ErrorAction SilentlyContinue
}

# --- recording assets (map identity references) --------------------------------
$assetsIn = Join-Path $root "recording-assets"
if (Test-Path $assetsIn) {
    Copy-Item $assetsIn (Join-Path $out "recording-assets") -Recurse
}

# --- summary ------------------------------------------------------------------
$files = Get-ChildItem $out -Recurse -File
$totalMB = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "Copied $($files.Count) files ($totalMB MB)." -ForegroundColor Green
Write-Host ""
Write-Host "To distribute:"
Write-Host "  1. zip the $OutDir folder"
Write-Host "  2. the recipient unzips it and runs:"
Write-Host "       powershell -ExecutionPolicy Bypass -File install.ps1"
Write-Host "     (add -Yolo for the mob-detection environment)"
Write-Host "  3. then double-click 启动助手.bat to start." -ForegroundColor Cyan
Write-Host ""

if ($Zip) {
    $zipPath = Join-Path $root "release\MapleAssistant.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path $out -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host "Zipped to $zipPath" -ForegroundColor Green
}
