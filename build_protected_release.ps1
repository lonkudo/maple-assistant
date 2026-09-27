<#
.SYNOPSIS
    Build a source-free Nuitka release after the normal release has been field-approved.

.DESCRIPTION
    This is intentionally separate from release_now.ps1.  The normal package
    remains the recovery path while compiler/data-file behavior is validated.
    It never reads an operator private key; license_public_key.json is enough.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stage = Join-Path $root "work\protected-stage"
$out = Join-Path $root "release\MapleAssistant-Protected"
Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $out -Recurse -Force -ErrorAction SilentlyContinue

# Reuse the normal release's curated data layout first, then replace the
# Python entry point with a standalone executable.  This prevents operator
# tools/private key material from ever entering the protected package.
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "build_release.ps1") `
    -Version $Version -OutDir "work\protected-stage"
if ($LASTEXITCODE -ne 0) { throw "Could not stage protected release." }

Push-Location $root
try {
    & $Python -m nuitka --standalone --assume-yes-for-downloads --enable-plugin=tk-inter `
        "--output-dir=$out" "--output-filename=MapleAssistant.exe" `
        --include-data-dir=recording-assets=recording-assets `
        --include-data-dir=sound=sound `
        --include-data-dir=autolie_api=autolie_api `
        --include-data-file=license_public_key.json=license_public_key.json `
        --include-data-file=activation_server_pin.json=activation_server_pin.json `
        --include-data-file=VERSION=VERSION `
        assistant.py
    if ($LASTEXITCODE -ne 0) { throw "Nuitka compilation failed." }
} finally {
    Pop-Location
}

$dist = Get-ChildItem -LiteralPath $out -Directory -Filter "assistant.dist" |
    Select-Object -First 1
if ($null -eq $dist) { throw "Nuitka output directory was not found." }

# The protected executable includes its Python runtime, so it needs no venv
# installer.  Keep the familiar launcher names, but launch the compiled app
# directly rather than the source-package launcher (which expects assistant.py).
$launcher = @"
@echo off
start "" "%~dp0MapleAssistant.exe" %*
"@
[System.IO.File]::WriteAllText(
    # Use Unicode code points instead of a Chinese string literal.  Windows
    # PowerShell can otherwise decode this script with the active ANSI code
    # page and create a mojibake launcher name in the release directory.
    (Join-Path $dist.FullName (([char]0x542F).ToString() + ([char]0x52A8) + ([char]0x52A9) + ([char]0x624B) + ".bat")), $launcher,
    [System.Text.Encoding]::ASCII
)
[System.IO.File]::WriteAllText(
    (Join-Path $dist.FullName "start_assistant.bat"), $launcher,
    [System.Text.Encoding]::ASCII
)

# Ship the complete standalone directory.  The executable's embedded Python
# runtime lives beside MapleAssistant.exe, so distributing only the .exe would
# be broken.  Operator-side license inventory/private material was never
# copied into this directory by build_release.ps1.
$zip = Join-Path $root "release\MapleAssistant-EXE-$Version.zip"
Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $dist.FullName "*") -DestinationPath $zip -Force
Get-ChildItem (Join-Path $root "release") -File -Filter "MapleAssistant-EXE-*.zip" |
    Where-Object { $_.FullName -ne $zip } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
Write-Host "Protected EXE release ready: $zip" -ForegroundColor Green
