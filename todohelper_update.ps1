<#
.SYNOPSIS
    Persistent MoonEclipse restart and ZIP-update helper.

.DESCRIPTION
    This script ships with every package.  The running application invokes it
    instead of generating a new helper for each update.  It waits for the
    current process to exit, optionally replaces the install directory from a
    validated compatible ZIP, then uses the package launcher to start again.
#>
param(
    [ValidateSet("Restart", "Update")]
    [string]$Mode = "Restart",
    [Parameter(Mandatory = $true)] [int]$TargetPid,
    [Parameter(Mandatory = $true)] [string]$InstallRoot,
    [string]$PackagePath = "",
    [string]$ConfigSource = "",
    [ValidateSet("source", "frozen")] [string]$PackageFormat = "source"
)

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($InstallRoot)
$log = Join-Path $root "update-helper.log"

function Write-UpdateLog([string]$message) {
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message" -Encoding UTF8
}

function Wait-ForTarget([int]$targetProcessId) {
    # Two minutes: a patrol stop, a shutdown save and an antivirus scan of the
    # exiting image can all take a while, and giving up early meant the whole
    # update was abandoned with nothing installed.
    for ($i = 0; $i -lt 1200; $i++) {
        # $PID is a PowerShell automatic, read-only variable.  Do not name a
        # function parameter $pid: parameter binding would fail before this
        # helper has a chance to wait for the assistant and restart it.
        if (-not (Get-Process -Id $targetProcessId -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 100
    }
    throw "Timed out waiting for MoonEclipse to exit."
}

function Show-OperatorMessage([string]$message) {
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        [System.Windows.Forms.MessageBox]::Show(
            $message, "MoonEclipse update", 0, 16) | Out-Null
    } catch {
        # No interactive desktop: the log is the only channel left.
        Write-UpdateLog "MESSAGE (no window): $message"
    }
}

function Find-PackageRoot([string]$stage, [string]$format) {
    $candidates = @(Get-ChildItem -LiteralPath $stage -Recurse -File -Filter VERSION |
        ForEach-Object { $_.Directory } |
        Where-Object {
            if ($format -eq "frozen") {
                (Test-Path (Join-Path $_ "MoonEclipse.exe")) -or
                (Test-Path (Join-Path $_ "TodoHelper.exe")) -or
                (Test-Path (Join-Path $_ "MapleAssistant.exe"))
            } else {
                Test-Path (Join-Path $_ "assistant.py")
            }
        })
    if ($candidates.Count -ne 1) { throw "Update ZIP has no unambiguous compatible MoonEclipse package." }
    return $candidates[0].FullName
}

function Copy-Package([string]$source, [string]$destination) {
    Get-ChildItem -LiteralPath $source -Recurse -Force | ForEach-Object {
        $relative = $_.FullName.Substring($source.Length).TrimStart('\')
        if (-not $relative -or $relative -match '^(\.git|\.venv|work|node_modules)(\\|$)') { return }
        # The live recording (layers, endpoints, jump points) is the "recording"
        # section of the config folder's user_config.json, and authorization is
        # auth.json / cim_identity.dat.  A package ships its own copy of that
        # user_config.json template, so compare the file NAME: matching only the
        # package-root path replaced the operator's recording with the template.
        if (-not $_.PSIsContainer -and (
                $_.Name -ieq "user_config.json" -or
                $_.Name -ieq "auth.json" -or
                $_.Name -ieq "cim_identity.dat")) { return }
        $target = Join-Path $destination $relative
        if ($_.PSIsContainer) {
            New-Item -ItemType Directory -Path $target -Force | Out-Null
        } else {
            New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        }
    }
}

function Install-ImportedConfig([string]$sourcePath, [string]$destinationRoot) {
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Imported configuration is missing: $sourcePath"
    }
    try {
        $payload = Get-Content -LiteralPath $sourcePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $payload -or $payload -is [System.Array]) {
            throw "root must be a JSON object"
        }
    } catch {
        throw "Imported configuration is not valid JSON: $($_.Exception.Message)"
    }
    $target = Join-Path $destinationRoot "user_config.json"
    $temporary = Join-Path $destinationRoot (".user_config.json." + [guid]::NewGuid().ToString("N") + ".importing")
    Copy-Item -LiteralPath $sourcePath -Destination $temporary -Force
    try {
        $lastError = $null
        for ($i = 0; $i -lt 150; $i++) {
            try {
                # File.Replace preserves the all-or-nothing handoff when the
                # destination already exists.  The old MoonEclipse process was
                # waited out above, so this retry only covers a short-lived
                # external handle such as antivirus indexing.
                if (Test-Path -LiteralPath $target -PathType Leaf) {
                    [System.IO.File]::Replace($temporary, $target, $null)
                } else {
                    [System.IO.File]::Move($temporary, $target)
                }
                Write-UpdateLog "imported user configuration after previous instance exited"
                return
            } catch {
                $lastError = $_
                Start-Sleep -Milliseconds 200
            }
        }
        throw "Could not replace user configuration after shutdown: $($lastError.Exception.Message)"
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

try {
    # Log every argument, not just the mode: when an install fails the operator
    # only ever sends this file, and "path does not exist" is worthless without
    # the paths the helper was actually given.
    Write-UpdateLog ("helper started mode=" + $Mode + " pid=" + $TargetPid +
        " root=" + $root + " format=" + $PackageFormat +
        " package=" + $(if ($PackagePath) { $PackagePath } else { "(none)" }))
    Wait-ForTarget $TargetPid
} catch {
    Write-UpdateLog "FAILED: $($_.Exception.Message)"
    Show-OperatorMessage ("MoonEclipse did not exit, so the update was not installed." + [Environment]::NewLine +
        "Close the assistant and click the update icon again, or unpack the package by hand.")
    exit 1
}

function Resolve-PackagePath([string]$given, [string]$installRoot) {
    # The application hands over the archive it downloaded into ``update``.  If
    # that exact file is gone - a hand-copied package, a second click, a cleaner
    # - the operator still meant to install SOME release, so fall back to the
    # newest ZIP the install can see and log the substitution instead of failing
    # with the bare "path does not exist" this helper used to show.
    if ($given -and (Test-Path -LiteralPath $given -PathType Leaf)) { return $given }
    $searched = @()
    $candidates = @()
    foreach ($folder in @((Join-Path $installRoot "update"), $installRoot, (Split-Path $installRoot))) {
        if (-not $folder -or -not (Test-Path -LiteralPath $folder -PathType Container)) { continue }
        $searched += $folder
        $candidates += @(Get-ChildItem -LiteralPath $folder -File -Filter "*.zip" -ErrorAction SilentlyContinue)
    }
    if (-not $candidates) {
        throw ("Update ZIP is missing: '" + $given + "'. Searched: " + ($searched -join "; "))
    }
    $newest = $candidates | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Write-UpdateLog ("package path '" + $given + "' was not found; using '" + $newest.FullName +
        "' (newest ZIP in " + ($searched -join "; ") + ")")
    return $newest.FullName
}

function Read-InstalledVersion([string]$installRoot) {
    $file = Join-Path $installRoot "VERSION"
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return "" }
    try { return (Get-Content -LiteralPath $file -Raw -ErrorAction Stop).Trim() } catch { return "" }
}

$installFailure = ""
if ($Mode -eq "Update") {
    # The application has already exited, so a failing install must not abort
    # this helper: the operator would be left with no running program at all
    # ("the update finished but the client never came back").  Keep the error,
    # finish whatever is possible, and always try to restart.
    #
    # Every step is named in its own error: the operator only ever reports the
    # message box, and "C ... because that path does not exist" without knowing
    # whether it came from extracting, locating or copying tells nobody anything.
    $step = "start"
    $versionBefore = Read-InstalledVersion $root
    Write-UpdateLog ("install: installed version before = '" + $versionBefore + "'")
    try {
        $step = "resolve package"
        $PackagePath = Resolve-PackagePath $PackagePath $root
        $step = "extract " + $PackagePath
        $stage = Join-Path ([IO.Path]::GetTempPath()) ("todohelper-update-" + [guid]::NewGuid().ToString("N"))
        try {
            Expand-Archive -LiteralPath $PackagePath -DestinationPath $stage -Force
            $step = "locate the package root in " + $stage
            $source = Find-PackageRoot $stage $PackageFormat
            $packageVersion = Read-InstalledVersion $source
            Write-UpdateLog ("install: package root=" + $source + " package version='" + $packageVersion + "'")
            $step = "copy " + $source + " -> " + $root
            Copy-Package $source $root
            $versionAfter = Read-InstalledVersion $root
            Write-UpdateLog ("install: installed version after = '" + $versionAfter + "'")
            if ($packageVersion -and $versionAfter -and ($packageVersion -ne $versionAfter)) {
                throw ("VERSION is still '" + $versionAfter + "' after copying the package '" +
                    $packageVersion + "' (a file in " + $root + " was not replaced)")
            }
            $step = "consume " + $PackagePath
            # Cosmetics only: removing the downloaded archive must never be
            # reported as "the update was not installed".
            try {
                Remove-Item -LiteralPath $PackagePath -Force -ErrorAction Stop
                Write-UpdateLog "update applied and consumed ZIP=$PackagePath"
            } catch {
                Write-UpdateLog ("WARNING: the update was installed but the package could not be removed (" +
                    $_.Exception.Message + ")")
            }
        } finally {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
    } catch {
        $installFailure = ("[" + $step + "] " + $_.Exception.Message)
        Write-UpdateLog "INSTALL FAILED (current installation kept, restarting anyway): $installFailure"
    }
}
if ($ConfigSource) {
    try {
        Install-ImportedConfig $ConfigSource $root
    } catch {
        Write-UpdateLog "CONFIG IMPORT FAILED: $($_.Exception.Message)"
    }
}

# Restart, whatever happened above.  The packaged executable is tried FIRST:
# the launcher asks Windows for elevation ("runas"), which turns an automatic
# restart into a UAC prompt that can silently be denied - and this helper
# already runs at the same integrity level as the assistant it replaces, so a
# direct start reproduces the previous session without asking the operator.
#
# ``-WindowStyle Hidden`` is not cosmetic.  The frozen build is a console
# subsystem program, so a plain Start-Process gives the restarted assistant its
# own console window; the operator sees a cmd window that belongs to the new
# instance, and closing it terminates the assistant ("I closed the cmd window
# and the application closed").  The shipped launcher chain hides it, so the
# helper's own start must hide it too.
$started = ""
foreach ($name in @("MoonEclipse.exe", "TodoHelper.exe", "MapleAssistant.exe")) {
    $candidate = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    try {
        Start-Process -FilePath $candidate -WorkingDirectory $root -WindowStyle Hidden -ErrorAction Stop
        $started = $name
        break
    } catch {
        Write-UpdateLog "$name start failed: $($_.Exception.Message)"
    }
}
$launcher = Join-Path $root "launch_assistant.vbs"
if (-not $started -and (Test-Path -LiteralPath $launcher)) {
    try {
        Start-Process -FilePath "wscript.exe" -ArgumentList @("//nologo", $launcher) -WorkingDirectory $root -WindowStyle Hidden -ErrorAction Stop
        $started = "launch_assistant.vbs"
    } catch {
        Write-UpdateLog "launcher start failed: $($_.Exception.Message)"
    }
}
if (-not $started) {
    foreach ($name in @("start_assistant.bat")) {
        $candidate = Join-Path $root $name
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$candidate`"") -WorkingDirectory $root -WindowStyle Hidden -ErrorAction Stop
            $started = $name
            break
        } catch {
            Write-UpdateLog "$name start failed: $($_.Exception.Message)"
        }
    }
}
if ($started) {
    Write-UpdateLog "restart launched via $started"
    # An elevated launcher shows a UAC prompt first, so allow it time to appear
    # before judging the start as failed.
    Start-Sleep -Seconds 8
    $alive = @(Get-Process -Name "MoonEclipse", "pythonw", "python" -ErrorAction SilentlyContinue)
    if ($alive.Count -eq 0) {
        Write-UpdateLog "WARNING: no MoonEclipse process 8s after the restart (UAC declined, or the new install cannot start)"
    } else {
        $aliveIds = ($alive | ForEach-Object { $_.Id }) -join ","
        Write-UpdateLog "restart confirmed: pid=$aliveIds"
    }
} else {
    Write-UpdateLog "FAILED: no launcher could be started"
    Show-OperatorMessage ("MoonEclipse was not restarted: no usable launcher was found in " + $root + "." + [Environment]::NewLine +
        "Start MoonEclipse.exe from that folder and send update-helper.log to the developer.")
}
if ($installFailure) {
    Show-OperatorMessage ("The update was NOT installed; the previous version is running again." + [Environment]::NewLine +
        $installFailure + [Environment]::NewLine +
        "Details: update-helper.log in the install folder.")
}
