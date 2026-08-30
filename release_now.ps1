<#
.SYNOPSIS
    One-command release: run the release-gate tests, rebuild the distributable
    folder + four-digit versioned zip, and verify the zip. Everything the README
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
        test_character_worker test_config_store test_countdown_worker `
        test_lie_detector_worker test_screen_blinker `
        test_minimap_detector test_assistant test_single_instance `
        test_capture_worker test_map_identity test_map_structure_tracker `
        test_versioning test_installer_scripts `
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
$versionPath = Join-Path $root "VERSION"
$hadVersion = Test-Path -LiteralPath $versionPath
$previousVersion = if ($hadVersion) {
    Get-Content -LiteralPath $versionPath -Raw
} else { $null }
$versionOutput = @(& $python -u (Join-Path $root "versioning.py") `
    next $versionPath 2>&1)
if ($LASTEXITCODE -ne 0 -or $versionOutput.Count -eq 0) {
    $versionOutput | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    exit 1
}
$version = $versionOutput[-1].ToString().Trim()
if ($version -notmatch '^\d{4}$') {
    Write-Host "invalid next release version: $version" -ForegroundColor Red
    exit 1
}
[System.IO.File]::WriteAllText(
    $versionPath, "$version`n", [System.Text.Encoding]::ASCII
)
function Restore-VersionFile {
    if ($hadVersion) {
        [System.IO.File]::WriteAllText(
            $versionPath, $previousVersion, [System.Text.Encoding]::ASCII
        )
    } elseif (Test-Path -LiteralPath $versionPath) {
        Remove-Item -LiteralPath $versionPath -Force
    }
}
Write-Host "release version: v$version" -ForegroundColor Green
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
    -File $buildScript -Version $version -Zip 2>&1
if ($LASTEXITCODE -ne 0) {
    Restore-VersionFile
    Write-Host "build_release.ps1 FAILED" -ForegroundColor Red
    exit 1
}

$zip = Get-Item -LiteralPath (
    Join-Path $root "release\MapleAssistant-v$version.zip"
) -ErrorAction SilentlyContinue
if ($null -eq $zip) {
    Restore-VersionFile
    Write-Host "no zip was produced under release\" -ForegroundColor Red
    exit 1
}

Write-Host "== 3/3 verify ==" -ForegroundColor Cyan
& $python -u (Join-Path $root "work\verify_zip.py") $zip.FullName 2>&1
if ($LASTEXITCODE -ne 0) {
    Restore-VersionFile
    Remove-Item -LiteralPath $zip.FullName -Force -ErrorAction SilentlyContinue
    Write-Host "zip verification FAILED - do not ship" -ForegroundColor Red
    exit 1
}

Write-Host ("release ready: " + $zip.FullName) -ForegroundColor Green
exit 0
