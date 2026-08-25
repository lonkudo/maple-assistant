<#
.SYNOPSIS
    One-command release: run the release-gate tests, rebuild the distributable
    folder + timestamped zip, and verify the zip.  Everything the README
    "Releasing a new build" section does, in one step.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File release_now.ps1
    powershell -ExecutionPolicy Bypass -File release_now.ps1 -SkipTests
#>
param(
    [switch]$SkipTests
)

# Continue: the game workers log to stderr during tests, and Windows
# PowerShell 5.1 surfaces native stderr lines as non-terminating errors.
# Each step gates on $LASTEXITCODE instead, so that noise cannot abort the
# release pipeline.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$python = Join-Path $root "yolo-detection\venv313\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

$tempLog = Join-Path $root "work\release_gate.log"
New-Item -ItemType Directory -Path (Join-Path $root "work") -Force | Out-Null

if (-not $SkipTests) {
    Write-Host "== 1/3 release-gate tests ==" -ForegroundColor Cyan
    Push-Location $root
    & $python -u -m unittest `
        test_movement_worker test_ui_worker test_status_worker `
        test_minimap_detector test_assistant test_single_instance `
        test_capture_worker test_map_identity test_map_structure_tracker `
        *> $tempLog 2>&1
    $testExit = $LASTEXITCODE
    Pop-Location
    if ($testExit -ne 0) {
        Write-Host "release-gate tests FAILED - see $tempLog" -ForegroundColor Red
        exit 1
    }
    Write-Host "release-gate tests OK" -ForegroundColor Green
}

Write-Host "== 2/3 build + zip ==" -ForegroundColor Cyan
# build_release.ps1 must stay UTF-8 WITH BOM - Windows PowerShell 5.1 cannot
# parse its Chinese script text without it, and some editors strip the BOM.
# Repair automatically so the one-command flow always works.
$buildScript = Join-Path $root "build_release.ps1"
$bytes = [System.IO.File]::ReadAllBytes($buildScript)
if ($bytes.Length -lt 3 -or $bytes[0] -ne 0xEF -or $bytes[1] -ne 0xBB -or $bytes[2] -ne 0xBF) {
    $text = [System.IO.File]::ReadAllText($buildScript, [System.Text.Encoding]::UTF8)
    [System.IO.File]::WriteAllText(
        $buildScript, $text, (New-Object System.Text.UTF8Encoding $true)
    )
    Write-Host "build_release.ps1 BOM re-added (UTF-8 with BOM)" -ForegroundColor Yellow
}
& powershell -NoProfile -ExecutionPolicy Bypass `
    -File $buildScript -Zip 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "build_release.ps1 FAILED" -ForegroundColor Red
    exit 1
}

$zip = Get-ChildItem (Join-Path $root "release") -Filter "*-*.zip" |
    Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if ($null -eq $zip) {
    Write-Host "no zip was produced under release\" -ForegroundColor Red
    exit 1
}

Write-Host "== 3/3 verify ==" -ForegroundColor Cyan
& $python -u (Join-Path $root "work\verify_zip.py") $zip.FullName 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "zip verification FAILED - do not ship" -ForegroundColor Red
    exit 1
}

Write-Host ("release ready: " + $zip.FullName) -ForegroundColor Green
exit 0