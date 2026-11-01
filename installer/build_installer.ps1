<# Build the separate MoonEclipse installer executable; it is never included in app updates. #>
param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pythonExe = if ($Python) { $Python } elseif (Test-Path (Join-Path $root ".venv\Scripts\python.exe")) { Join-Path $root ".venv\Scripts\python.exe" } else { "python" }
$out = Join-Path $root "release\MoonEclipseInstaller.exe"
$work = Join-Path $root "work\installer-build"
Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $root "release\MoonEclipseInstaller") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $work | Out-Null

# The installer imports the pinned activation transport and fingerprint helper,
# so include their non-Python pin data explicitly.  The built EXE remains a
# separate bootstrap artifact and is never published inside MoonEclipse app
# manifests.
& $pythonExe -m nuitka --onefile --enable-plugin=tk-inter --windows-console-mode=disable `
    --output-dir=$work --output-filename=MoonEclipseInstaller.exe `
    --include-data-file="$root\activation_server_pin.json=activation_server_pin.json" `
    --include-data-file="$root\license_public_key.json=license_public_key.json" `
    --include-module=licensing --include-module=pinned_tls `
    (Join-Path $root "installer\MoonEclipseInstaller.py")
if ($LASTEXITCODE -ne 0) { throw "installer build failed" }
$built = Get-ChildItem -LiteralPath $work -File -Filter "MoonEclipseInstaller.exe" | Select-Object -First 1
if (-not $built) { throw "installer output missing" }
Copy-Item -LiteralPath $built.FullName -Destination $out -Force
Write-Host "Installer ready: $out" -ForegroundColor Green
