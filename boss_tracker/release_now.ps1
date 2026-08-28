$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $source

Write-Host "== 1/3 BOSS Tracker tests ==" -ForegroundColor Cyan
Push-Location $source
try {
    & py -3.10 -m unittest -v test_model test_audio
    if ($LASTEXITCODE -ne 0) { throw "BOSS Tracker tests failed" }
} finally {
    Pop-Location
}

Write-Host "== 2/3 build + zip ==" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $source "build_release.ps1") -Zip
if ($LASTEXITCODE -ne 0) { throw "BOSS Tracker build failed" }

Write-Host "== 3/3 verify ==" -ForegroundColor Cyan
$zip = Get-ChildItem (Join-Path $root "release") -Filter "BossTracker-*.zip" |
    Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if ($null -eq $zip) { throw "BOSS Tracker zip was not created" }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($zip.FullName)
try {
    $names = @($archive.Entries | ForEach-Object { $_.FullName })
    foreach ($required in @(
        "BossTracker\app.py", "BossTracker\audio.py",
        "BossTracker\model.py", "BossTracker\sound\beep.mp3",
        "BossTracker\requirements.txt",
        "BossTracker\vendor\comtypes\__init__.py"
    )) {
        if ($required -notin $names) { throw "zip missing $required" }
    }
    if (-not ($names | Where-Object { $_ -like "BossTracker\*.bat" })) {
        throw "zip missing launcher bat"
    }
    if ($names | Where-Object { $_ -like "*config.json" }) {
        throw "zip contains user config.json"
    }
} finally {
    $archive.Dispose()
}

Write-Host ("release ready: " + $zip.FullName) -ForegroundColor Green
