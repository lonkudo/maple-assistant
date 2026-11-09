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

# The installer imports the pinned activation transport, the fingerprint helper,
# the share resolver and the shared ``update_core`` download path, so include
# their non-Python pin data explicitly and put the repository root on the module
# path: ``update_core``/``lanzou_source`` live there, shared with the desktop
# application.  The built EXE remains a separate bootstrap artifact and is never
# published inside MoonEclipse app manifests.
#
# The imaging stack and the CT-service bridge are deliberately NOT followed.
# Nuitka walks imports statically, so ``licensing -> auto_lie_secret /
# local_lie_service -> autolie_api`` dragged the vendor package, cv2 (100 MB) and
# numpy (26 MB) into a bootstrap that only downloads a ZIP and activates a code -
# the installer grew to 47 MB instead of ~10 MB.  local_lie_service also pulled
# pywin32 (win32api/win32process/pythoncom), which a bootstrap has no use for.
# Every such call site is lazy and guarded (``from autolie_api.key_store import
# mask_key`` sits inside a try/except in auto_lie_secret; licensing imports
# local_lie_service only inside functions whose bodies catch Exception), so the
# installer's own work is unchanged while the payload stays a bootstrap.
$excludedModules = @(
    "autolie_api", "cv2", "numpy", "image_io", "lie_geometry",
    "lie_roi_geometry", "minimap_detector", "marker_detector", "map_identity",
    "local_lie_service", "local_lie_backend",
    "win32api", "win32process", "win32con", "win32gui", "pythoncom", "pywintypes",
    # The process pool only exists because concurrent.futures is imported for its
    # ThreadPoolExecutor; nothing in this bootstrap calls ProcessPoolExecutor.
    # Nuitka's own multiprocessing plugin is disabled for the same reason (leaving
    # it on makes the exclusion a FATAL conflict), which also drops the
    # multiprocessing -> xmlrpc -> xml.parsers.expat / decimal chain.
    "multiprocessing", "xmlrpc", "xml"
)
$excludeArgs = $excludedModules | ForEach-Object { "--nofollow-import-to=$_" }
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = if ($previousPythonPath) { "$root;$previousPythonPath" } else { $root }
try {
    & $pythonExe -m nuitka --onefile --enable-plugin=tk-inter --windows-console-mode=disable `
        --disable-plugin=multiprocessing `
        --output-dir=$work --output-filename=MoonEclipseInstaller.exe `
        @excludeArgs `
        --include-data-file="$root\activation_server_pin.json=activation_server_pin.json" `
        --include-data-file="$root\license_public_key.json=license_public_key.json" `
        --include-module=licensing --include-module=pinned_tls `
        --include-module=lanzou_source --include-module=update_core `
        --include-module=versioning `
        (Join-Path $root "installer\MoonEclipseInstaller.py")
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
if ($LASTEXITCODE -ne 0) { throw "installer build failed" }
$built = Get-ChildItem -LiteralPath $work -File -Filter "MoonEclipseInstaller.exe" | Select-Object -First 1
if (-not $built) { throw "installer output missing" }
Copy-Item -LiteralPath $built.FullName -Destination $out -Force
Write-Host "Installer ready: $out" -ForegroundColor Green
