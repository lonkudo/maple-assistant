param([switch]$Zip)

$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $source
$releaseRoot = Join-Path $root "release"
$out = Join-Path $releaseRoot "BossTracker"

if (Test-Path $out) { Remove-Item -LiteralPath $out -Recurse -Force }
New-Item -ItemType Directory -Path $out -Force | Out-Null

@("app.py", "audio.py", "model.py", "README.md", "requirements.txt", "install_boss_tracker.ps1", "launch_boss_tracker.vbs") |
    ForEach-Object { Copy-Item -LiteralPath (Join-Path $source $_) -Destination $out }
Get-ChildItem -LiteralPath $source -Filter "*.bat" |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $out }

$soundOut = Join-Path $out "sound"
New-Item -ItemType Directory -Path $soundOut -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $root "sound\beep.mp3") -Destination $soundOut

# Bundle the small pure-Python COM bridge so speech works immediately without
# requiring the recipient to install a package or run PowerShell TTS.
$vendorOut = Join-Path $out "vendor"
& py -3.10 -m pip install --disable-pip-version-check --no-compile `
    --target $vendorOut -r (Join-Path $source "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "could not bundle BOSS Tracker dependencies" }

if ($Zip) {
    Get-ChildItem -LiteralPath $releaseRoot -Filter "BossTracker-*.zip" |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $stamp = Get-Date -Format "dd-HH-mm"
    $zipPath = Join-Path $releaseRoot "BossTracker-$stamp.zip"
    Compress-Archive -LiteralPath $out -DestinationPath $zipPath `
        -CompressionLevel Optimal -Force
    Write-Host ("BOSS Tracker package: " + $zipPath) -ForegroundColor Green
}
