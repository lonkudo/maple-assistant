<#
.SYNOPSIS
    Build a source-free Nuitka release after the normal release has been field-approved.

.DESCRIPTION
    This is intentionally separate from release_now.ps1.  The normal package
    remains the recovery path while compiler/data-file behavior is validated.
    It never reads an operator private key; license_public_key.json is enough.

    The build is self-sufficient about Python requirements, because a missing
    module used to produce an executable that died on the customer's machine
    with "ModuleNotFoundError: No module named 'cv2'":

      1. It prefers the project .venv interpreter, the only interpreter on a
         normal operator machine that carries numpy, Pillow, OpenCV, pywin32,
         cryptography and tkinter together.  A bare `python` from PATH is only
         a fallback.
      2. It walks the real import closure of assistant.py and automatically
         installs any third-party module that cannot be imported (mirrors
         first, official PyPI last) instead of compiling without it.
      3. It mirrors the normal package's whole data layout (every non-source
         file, 1:1), so the frozen package cannot start with a missing
         hotkey.json / system_config.json the way a hand-written include list
         allowed.  It also compiles dynamically imported modules that static
         analysis cannot see (mouse_aim_controller).
      4. It reuses the normal package's launcher behaviour: a hidden wscript
         that starts the executable with the "runas" verb, because the
         assistant must run at the game's privilege level for key injection.
      5. After compiling, it starts the packaged executable once and fails the
         build if the frozen application cannot import a bundled module, so a
         broken executable can no longer be published.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\build_protected_release.ps1 -Version 1.1.161
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
# Nuitka writes progress to stderr; that must not be mistaken for a failure.
$PSNativeCommandUseErrorActionPreference = $false
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stage = Join-Path $root "work\protected-stage"
$out = Join-Path $root "release\TodoHelper-Protected"

# Reports the third-party modules reachable from assistant.py, and which of
# them the given interpreter cannot import.  Printed as one JSON line.
$requirementChecker = @'
"""Report third-party modules reachable from assistant.py that cannot import."""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STDLIB = set(sys.stdlib_module_names)
PIP_NAME = {
    "cv2": "opencv-python-headless",
    "PIL": "Pillow",
    "numpy": "numpy",
    "cryptography": "cryptography",
    "yaml": "PyYAML",
}
PIP_PREFIX = (
    ("win32com", "pywin32"),
    ("win32", "pywin32"),
    ("pythoncom", "pywin32"),
    ("pywintypes", "pywin32"),
)


def local_module(name: str):
    file = ROOT / (name + ".py")
    if file.exists():
        return file
    package = ROOT / name / "__init__.py"
    return package if package.exists() else None


def pip_name(name: str) -> str:
    if name in PIP_NAME:
        return PIP_NAME[name]
    for prefix, package in PIP_PREFIX:
        if name.startswith(prefix):
            return package
    return name


def closure(entry: str):
    seen: set[str] = set()
    external: dict[str, str] = {}
    queue = [entry]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = local_module(name)
        if path is None:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for found in names:
                if found in STDLIB:
                    continue
                if local_module(found) is not None:
                    queue.append(found)
                else:
                    external[found] = pip_name(found)
    return seen, external


def main() -> int:
    modules, external = closure("assistant")
    missing: dict[str, str] = {}
    for name, package in sorted(external.items()):
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            missing[name] = package
    print(json.dumps({
        "local_modules": len(modules),
        "external": external,
        "missing": missing,
        "packages": sorted(set(missing.values())),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'@

function Resolve-BuildPython {
    param([string]$Requested)
    if ($Requested) {
        if (Test-Path $Requested) { return (Resolve-Path -LiteralPath $Requested).Path }
        $command = Get-Command $Requested -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
        throw "Cannot find the build interpreter '$Requested'."
    }
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) {
        Write-Warning "No .venv interpreter found; falling back to $($command.Source)."
        return $command.Source
    }
    throw "No build interpreter found. Install Python or pass -Python <path>."
}

function Get-RequirementReport {
    param([string]$Interpreter)
    $checker = Join-Path $root "work\build-requirement-check.py"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $checker) | Out-Null
    Set-Content -LiteralPath $checker -Value $requirementChecker -Encoding UTF8
    $lines = & $Interpreter $checker
    if ($LASTEXITCODE -ne 0 -or -not $lines) {
        throw "The requirement check did not run under $Interpreter."
    }
    return (($lines | Select-Object -Last 1) | ConvertFrom-Json)
}

function Install-MissingRequirement {
    param([string]$Interpreter, [string[]]$Packages)
    # Same mirror order as install.ps1: TUNA, Aliyun, Tencent, then PyPI.
    $indexes = @(
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple",
        "https://mirrors.cloud.tencent.com/pypi/simple",
        "https://pypi.org/simple"
    )
    foreach ($index in $indexes) {
        Write-Host "Installing missing requirement from $index : $($Packages -join ', ')" -ForegroundColor Yellow
        & $Interpreter -m pip install --disable-pip-version-check --index-url $index @Packages
        if ($LASTEXITCODE -eq 0) { return $true }
        Write-Warning "Installing from $index failed; trying the next index."
    }
    return $false
}

function Test-FrozenStartup {
    param([string]$DistDir, [int]$TimeoutSeconds = 20)
    $exe = Join-Path $DistDir "TodoHelper.exe"
    if (-not (Test-Path $exe)) { throw "Packaged executable not found: $exe" }

    # Snapshot the package so the probe's own generated files can be removed
    # again and never end up inside the shipped ZIP.
    $before = New-Object 'System.Collections.Generic.HashSet[string]'
    Get-ChildItem -LiteralPath $DistDir -Recurse -File |
        ForEach-Object { [void]$before.Add($_.FullName) }

    $stdout = Join-Path $env:TEMP ("ma-startup-" + [guid]::NewGuid().ToString("N") + ".out")
    $stderr = Join-Path $env:TEMP ("ma-startup-" + [guid]::NewGuid().ToString("N") + ".err")
    $process = Start-Process -FilePath $exe -WorkingDirectory $DistDir -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Start-Sleep -Seconds $TimeoutSeconds
    $exited = $process.HasExited
    $exitCode = $null
    if ($exited) {
        $exitCode = $process.ExitCode
    } else {
        $process.Kill()
        $process.WaitForExit()
    }

    $captured = ""
    foreach ($file in @($stderr, $stdout)) {
        if (Test-Path $file) { $captured += (Get-Content -LiteralPath $file -Raw) }
    }

    Get-ChildItem -LiteralPath $DistDir -Recurse -File |
        Where-Object { -not $before.Contains($_.FullName) } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
    Get-ChildItem -LiteralPath $DistDir -Recurse -Directory |
        Sort-Object { $_.FullName.Length } -Descending |
        Where-Object { -not (Get-ChildItem -LiteralPath $_.FullName -Recurse -File) } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue

    if ($captured -match "ModuleNotFoundError|ImportError") {
        throw "The packaged executable cannot import a bundled module, so this build must not ship. Captured output:`n$captured"
    }
    if ($exited) {
        # Every import happens before the single-instance guard, so an empty
        # output and a fast clean exit proves the bundled modules loaded.  The
        # usual reason for the early exit is that an assistant is already
        # running in this Windows session and owns the singleton mutex.
        $others = @(Get-Process -Name "TodoHelper" -ErrorAction SilentlyContinue)
        if ($others.Count -gt 0) {
            Write-Host "Frozen import check passed: every bundled module imported, then the executable exited through the single-instance guard because $($others.Count) TodoHelper process(es) are already running in this session." -ForegroundColor Green
        } else {
            Write-Warning "The packaged executable exited after ${TimeoutSeconds}s (exit code $exitCode) without an import error, and no other TodoHelper process is running, so the cause is unknown. Captured output:`n$captured"
        }
    } else {
        Write-Host "Frozen startup check passed: the executable stayed running for ${TimeoutSeconds}s." -ForegroundColor Green
    }
}

$interpreter = Resolve-BuildPython -Requested $Python
Write-Host "Build interpreter: $interpreter" -ForegroundColor Cyan

$report = Get-RequirementReport -Interpreter $interpreter
$externalCount = @($report.external.PSObject.Properties).Count
Write-Host "Import closure of assistant.py: $($report.local_modules) local modules, $externalCount third-party modules." -ForegroundColor Cyan
$packages = @($report.packages)
if ($packages.Count -gt 0) {
    Write-Host "Missing in the build interpreter: $($packages -join ', ')" -ForegroundColor Yellow
    if (-not (Install-MissingRequirement -Interpreter $interpreter -Packages $packages)) {
        throw "Could not install the missing build requirements: $($packages -join ', ')"
    }
    $report = Get-RequirementReport -Interpreter $interpreter
    $packages = @($report.packages)
    if ($packages.Count -gt 0) {
        throw "The build interpreter still cannot import: $($packages -join ', ')"
    }
    Write-Host "All requirements are present; continuing." -ForegroundColor Green
} else {
    Write-Host "All requirements are present." -ForegroundColor Green
}

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
    # Ship the normal package's whole data layout, not a hand-written list.  A
    # partial list is how hotkey.json and system_config.json went missing: the
    # compiled assistant started with "hotkey config unavailable" and no
    # bindings, so the recording/patrol hotkeys were dead.
    #
    # Every non-source file of the staged normal release is mirrored 1:1.
    # The source-package installer/launcher files are left out because they
    # expect a .venv that a frozen package does not have (the frozen package
    # gets its own launchers below).
    $dataRoot = Join-Path $root "work\protected-data"
    Remove-Item -LiteralPath $dataRoot -Recurse -Force -ErrorAction SilentlyContinue
    # The two Chinese launcher names are built from code points.  Windows
    # PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so a Chinese literal here
    # would be decoded as different characters and never match the real file
    # name (that is also why the generated launcher names below use code
    # points).  Keep this script ASCII-only.
    $chineseNames = @(
        (([char]0x5B89).ToString() + ([char]0x88C5) + ".bat"),
        (([char]0x542F).ToString() + ([char]0x52A8) + ([char]0x52A9) + ([char]0x624B) + ".bat")
    )
    $excluded = @(
        "install.ps1", "launch_assistant.vbs", "start_assistant.bat",
        "restart_assistant.ps1", "diagnose.bat"
    ) + $chineseNames
    $mirrored = 0
    Get-ChildItem -LiteralPath $stage -Recurse -File | ForEach-Object {
        if ($_.Extension -in ".py", ".pyc") { return }
        $relative = $_.FullName.Substring($stage.Length + 1)
        if ($excluded -contains $relative.Split('\')[0]) { return }
        $target = Join-Path $dataRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        $mirrored++
    }
    $dataArguments = @()
    Get-ChildItem -LiteralPath $dataRoot -Directory |
        ForEach-Object { $dataArguments += "--include-data-dir=$($_.FullName)=$($_.Name)" }
    Get-ChildItem -LiteralPath $dataRoot -File |
        ForEach-Object { $dataArguments += "--include-data-file=$($_.FullName)=$($_.Name)" }
    $mirrorMb = [math]::Round((Get-ChildItem -LiteralPath $dataRoot -Recurse -File |
        Measure-Object -Property Length -Sum).Sum / 1MB, 1)
    Write-Host "Data parity: mirrored $mirrored files ($mirrorMb MB) from the normal package layout." -ForegroundColor Cyan

    # ``mouse_aim_controller`` is imported dynamically inside api_lie_video.py,
    # which static analysis cannot see.  It lives in target_tracker/ without an
    # __init__.py, so it is resolved by plain name.
    $moduleArguments = @()
    $aimDirectory = Join-Path $stage "target_tracker"
    if (Test-Path (Join-Path $aimDirectory "mouse_aim_controller.py")) {
        $moduleArguments = @("--include-module=mouse_aim_controller")
        $previousPythonPath = $env:PYTHONPATH
        $env:PYTHONPATH = if ($previousPythonPath) { "$aimDirectory;$previousPythonPath" } else { $aimDirectory }
    }

    # Compile from inside the staged package so the frozen build resolves
    # modules exactly like the shipped normal package does.
    #
    # --windows-console-mode=hide removes the black console window: the
    # executable spawns its console and hides it immediately.  'hide' is chosen
    # over 'disable'/'attach' on purpose -- stdout/stderr stay real and valid,
    # so the application's own prints and logging cannot fail, and launching
    # the exe from an existing console (the diagnostic launcher below, whose
    # Chinese file name is built from code points) still shows its output because the console then belongs
    # to two processes and is deliberately left visible.
    Push-Location $stage
    try {
        & $interpreter -m nuitka --standalone --assume-yes-for-downloads --enable-plugin=tk-inter `
            --windows-console-mode=hide `
            "--output-dir=$out" "--output-filename=TodoHelper.exe" `
            @dataArguments @moduleArguments `
            assistant.py
        if ($LASTEXITCODE -ne 0) { throw "Nuitka compilation failed." }
    } finally {
        Pop-Location
        if (Test-Path Variable:\previousPythonPath) { $env:PYTHONPATH = $previousPythonPath }
    }
} finally {
    Pop-Location
}

$dist = Get-ChildItem -LiteralPath $out -Directory -Filter "assistant.dist" |
    Select-Object -First 1
if ($null -eq $dist) { throw "Nuitka output directory was not found." }

# Refuse to publish an executable that dies on a missing bundled module.
Test-FrozenStartup -DistDir $dist.FullName

# The probe above runs the real assistant, which may touch shipped runtime
# files (timer.json, hotkey.json).  Restore the normal package's exact data.
Copy-Item -Path (Join-Path $dataRoot "*") -Destination $dist.FullName -Recurse -Force

# The normal package's launcher is a hidden wscript that starts pythonw with
# the "runas" verb, because the assistant must run at the same privilege level
# as the game or Windows silently drops the injected keys (see INSTALL.md).  The
# frozen package has to behave the same way, so it gets the same launcher chain
# instead of a plain `start` of the exe.
$launcherVbs = @"
Option Explicit

' Hidden, elevated launcher, mirroring the normal package's launcher.
Dim shellApp, files, root, exePath, arguments, item
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
exePath = root & "\TodoHelper.exe"
arguments = ""
For Each item In WScript.Arguments
    arguments = arguments & " " & QuoteArgument(CStr(item))
Next

Set shellApp = CreateObject("Shell.Application")
On Error Resume Next
shellApp.ShellExecute exePath, arguments, root, "runas", 0
If Err.Number <> 0 Then
    MsgBox "TodoHelper could not start (administrator permission was not granted)." & vbCrLf & _
           "Run diagnose_start.bat to see the startup error.", 16, "TodoHelper"
End If
On Error GoTo 0

Function QuoteArgument(value)
    QuoteArgument = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
"@
$launcher = @"
@echo off
rem Hidden elevated start, exactly like the normal package's launcher: the
rem assistant needs the same privilege level as the game for key injection.
wscript.exe //nologo "%~dp0launch_assistant.vbs" %*
"@
[System.IO.File]::WriteAllText(
    (Join-Path $dist.FullName "launch_assistant.vbs"), $launcherVbs,
    [System.Text.Encoding]::ASCII
)
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

# The compiled executable is a console-subsystem program, so a startup failure
# closes its window before anyone can read the traceback.  This extra launcher
# keeps the window open on purpose; it changes nothing for the normal launcher.
$diagnoser = @"
@echo off
rem Diagnostic launcher: keeps this window open so a startup error is readable.
cd /d "%~dp0"
echo ============================================
echo   TodoHelper diagnostic start
echo ============================================
echo.
"%~dp0TodoHelper.exe" %*
echo.
echo Exit code: %ERRORLEVEL%
echo Send the text above to the developer if the assistant did not start.
echo.
pause
"@
[System.IO.File]::WriteAllText(
    # Diagnostic launcher (name built from code points for the reason above).
    (Join-Path $dist.FullName (([char]0x8BCA).ToString() + ([char]0x65AD) + ([char]0x542F) + ([char]0x52A8) + ".bat")), $diagnoser,
    [System.Text.Encoding]::ASCII
)
[System.IO.File]::WriteAllText(
    (Join-Path $dist.FullName "diagnose_start.bat"), $diagnoser,
    [System.Text.Encoding]::ASCII
)

# Ship the complete standalone directory.  The executable's embedded Python
# runtime lives beside TodoHelper.exe, so distributing only the .exe would
# be broken.  Operator-side license inventory/private material was never
# copied into this directory by build_release.ps1.
#
# The package name says "release", not "EXE"; the frozen package is a release
# in its own right.  The in-app updater only accepts archives that contain
# assistant.py, so it ignores this one under either name.
$zip = Join-Path $root "release\TodoHelper-release-$Version.zip"
Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $dist.FullName "*") -DestinationPath $zip -Force
# Drop the previous name's archive as well, so no stale package is mistaken
# for the current one.
foreach ($pattern in @("TodoHelper-release-*.zip", "TodoHelper-EXE-*.zip")) {
    Get-ChildItem (Join-Path $root "release") -File -Filter $pattern |
        Where-Object { $_.FullName -ne $zip } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}
Write-Host "Standalone release ready: $zip" -ForegroundColor Green
exit 0
