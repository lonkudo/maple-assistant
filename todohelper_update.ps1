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

function Get-RunningAssistantIds([string]$installRoot) {
    # EVERY assistant instance that can hold the singleton mutex or the packaged
    # .pyd files, not just the one that asked for the update.  The helper used to
    # wait for -TargetPid only; a second instance (left over from an earlier
    # restart, or started by hand with 启动助手) then kept $root\endpoints_
    # protected.pyd open - the 1.3.14/1.3.29 logs - and, worse, kept
    # Local\TodoHelper.Singleton.v1, so the freshly started instance hit the
    # singleton guard and exited silently ("restart failed", 1.3.33/1.3.34).
    $ids = @()
    foreach ($name in @("MoonEclipse", "TodoHelper", "MapleAssistant")) {
        $ids += @(Get-Process -Name $name -ErrorAction SilentlyContinue |
            ForEach-Object { [int]$_.Id })
    }
    # A source-layout install runs pythonw.exe startup_probe.py from this root.
    # ``Get-Process`` objects expose no CommandLine, so ask CIM for those two
    # names only (a whole-process WMI query would be needlessly slow here).
    try {
        $pythons = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction Stop)
        foreach ($process in $pythons) {
            $commandLine = [string]$process.CommandLine
            if ($commandLine -and
                    $commandLine -like "*startup_probe.py*" -and
                    $commandLine -like ("*" + $installRoot + "*")) {
                $ids += [int]$process.ProcessId
            }
        }
    } catch {
        Write-UpdateLog ("could not enumerate python processes: " + $_.Exception.Message)
    }
    return @($ids | Where-Object { $_ -and $_ -ne $PID -and $_ -ne $TargetPid } |
        Sort-Object -Unique)
}

function Stop-RunningAssistants([string]$installRoot) {
    $stopped = @()
    foreach ($id in @(Get-RunningAssistantIds $installRoot)) {
        try {
            Stop-Process -Id $id -Force -ErrorAction Stop
            $stopped += $id
        } catch {
            Write-UpdateLog ("could not stop pid " + $id + ": " + $_.Exception.Message)
        }
    }
    if ($stopped.Count -gt 0) {
        Write-UpdateLog ("stopped other assistant instance(s): " + ($stopped -join ","))
    }
    return $stopped
}

function Wait-ForTarget([int]$targetProcessId, [string]$installRoot) {
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

function Wait-ForNoAssistants([string]$installRoot, [int]$seconds = 10) {
    $deadline = [DateTime]::UtcNow.AddSeconds($seconds)
    do {
        if (@(Get-RunningAssistantIds $installRoot).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    return (@(Get-RunningAssistantIds $installRoot).Count -eq 0)
}

function Show-OperatorMessage([string]$message) {
    # The helper runs with CREATE_NO_WINDOW.  A BLOCKING MessageBox used to sit
    # invisible behind the game (or after this process ended), so the operator
    # only saw "the app closed and did not come back".  Log first, then show a
    # message that gives up by itself.
    Write-UpdateLog "MESSAGE: $message"
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup($message, 20, "MoonEclipse update", 48)
    } catch {
        try {
            Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
            [System.Windows.Forms.MessageBox]::Show(
                $message, "MoonEclipse update", 0, 16) | Out-Null
        } catch {
            # No interactive desktop: the log is the only channel left.
            Write-UpdateLog "MESSAGE (no window): $message"
        }
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
    # A file the exiting/lingering assistant still maps (the packaged
    # endpoints_protected.pyd is the one the field logs name) fails the whole
    # copy.  Retry that file for a few seconds before giving up, so a slow
    # unmapping never leaves the package half applied.
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
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
        if ($_.PSIsContainer) {
            $target = Join-Path $destination $relative
            New-Item -ItemType Directory -Path $target -Force | Out-Null
            return
        }
        # This very script is running from the destination.  Replacing it while
        # PowerShell still holds it fails the whole copy, and a failed copy is
        # what restarts a half-applied install.  Stage it and swap it in at the
        # very end, after the new instance is up.
        if ($relative -ieq "todohelper_update.ps1") {
            $pending = Join-Path $destination "todohelper_update.ps1.new"
            try {
                Copy-Item -LiteralPath $_.FullName -Destination $pending -Force -ErrorAction Stop
                $script:pendingSupervisorUpdate = $pending
                Write-UpdateLog "deferred the supervisor script update to the end of this run"
            } catch {
                Write-UpdateLog ("WARNING: the supervisor script could not be staged: " + $_.Exception.Message)
            }
            return
        }
        $target = Join-Path $destination $relative
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        for ($attempt = 1; ; $attempt++) {
            try {
                Copy-Item -LiteralPath $_.FullName -Destination $target -Force -ErrorAction Stop
                return
            } catch {
                $locked = $_.Exception.Message -match 'another process|使用|being used'
                if (-not $locked -or [DateTime]::UtcNow -ge $deadline) {
                    throw
                }
                if ($attempt -eq 1) {
                    Write-UpdateLog ("retrying " + $relative + " while another process releases it")
                }
                Start-Sleep -Milliseconds 500
            }
        }
    }
}

function Backup-ReplacedFiles([string]$source, [string]$destination, [string]$backupRoot) {
    # Snapshot every file the package is about to overwrite, so a copy that dies
    # half way (a locked .pyd, a dropped drive, a full disk) can be rolled back to
    # the exact previous installation instead of restarting a mixed tree - the
    # "the instance started after the update flashes closed" report.
    $copied = 0
    Get-ChildItem -LiteralPath $source -Recurse -Force -File | ForEach-Object {
        $relative = $_.FullName.Substring($source.Length).TrimStart('\')
        if (-not $relative -or $relative -match '^(\.git|\.venv|work|node_modules)(\\|$)') { return }
        if ($_.Name -ieq "user_config.json" -or $_.Name -ieq "auth.json" -or
                $_.Name -ieq "cim_identity.dat") { return }
        $existing = Join-Path $destination $relative
        if (-not (Test-Path -LiteralPath $existing -PathType Leaf)) { return }
        $target = Join-Path $backupRoot $relative
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        try {
            Copy-Item -LiteralPath $existing -Destination $target -Force -ErrorAction Stop
            $copied++
        } catch {
            throw ("could not back up " + $relative + " before installing: " + $_.Exception.Message)
        }
    }
    Write-UpdateLog ("backup: " + $copied + " file(s) -> " + $backupRoot)
    return $copied
}

function Restore-Backup([string]$backupRoot, [string]$destination) {
    # Put the snapshot back.  Best effort per file: a file that cannot be
    # restored is reported, and the rest of the install still becomes whole.
    if (-not (Test-Path -LiteralPath $backupRoot -PathType Container)) {
        Write-UpdateLog "restore: no backup to restore"
        return 0
    }
    $restored = 0
    foreach ($file in @(Get-ChildItem -LiteralPath $backupRoot -Recurse -Force -File)) {
        $relative = $file.FullName.Substring($backupRoot.Length).TrimStart('\')
        $target = Join-Path $destination $relative
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        $ok = $false
        for ($attempt = 1; $attempt -le 20 -and -not $ok; $attempt++) {
            try {
                Copy-Item -LiteralPath $file.FullName -Destination $target -Force -ErrorAction Stop
                $ok = $true
                $restored++
            } catch {
                Start-Sleep -Milliseconds 300
            }
        }
        if (-not $ok) {
            Write-UpdateLog ("restore: could not put back " + $relative)
        }
    }
    Write-UpdateLog ("restore: " + $restored + " file(s) restored from " + $backupRoot)
    return $restored
}

function Complete-DeferredSupervisorUpdate() {
    # Swap the staged supervisor script in now that nothing needs it.  A failure
    # here is harmless: the previous script also performs this hand-over, so the
    # next update simply stages it again.
    $pending = $script:pendingSupervisorUpdate
    if (-not $pending) { return }
    if (-not (Test-Path -LiteralPath $pending -PathType Leaf)) { return }
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        try {
            Move-Item -LiteralPath $pending -Destination $PSCommandPath -Force -ErrorAction Stop
            Write-UpdateLog "supervisor script updated to the packaged version"
            return
        } catch {
            Start-Sleep -Milliseconds 300
        }
    }
    Write-UpdateLog ("WARNING: the packaged supervisor script stays as " + $pending +
        " (the running one is still mapped); it is applied by the next update")
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

# ONE supervisor at a time, and this is the first thing it decides: the block
# below kills assistant instances, so a second supervisor that ran concurrently
# with the first would terminate the instance the first one had just started -
# the "instance started after the update flashes closed" report.
$supervisorLock = $null
$supervisorLockOwned = $false
try {
    $supervisorLock = New-Object System.Threading.Mutex($false, "Local\TodoHelper.UpdateSupervisor.v1")
    $supervisorLockOwned = $supervisorLock.WaitOne(3000)
} catch {
    Write-UpdateLog ("supervisor lock unavailable: " + $_.Exception.Message)
}
if (-not $supervisorLockOwned) {
    Write-UpdateLog ("another update supervisor already holds the lock; this one exits without touching " +
        "anything (mode=" + $Mode + " pid=" + $TargetPid + ")")
    Write-UpdateLog "supervisor finished: nothing done (single supervisor)"
    exit 2
}

try {
    # Log every argument, not just the mode: when an install fails the operator
    # only ever sends this file, and "path does not exist" is worthless without
    # the paths the helper was actually given.
    Write-UpdateLog ("helper started mode=" + $Mode + " pid=" + $TargetPid +
        " root=" + $root + " format=" + $PackageFormat +
        " package=" + $(if ($PackagePath) { $PackagePath } else { "(none)" }))
    Wait-ForTarget $TargetPid $root
    # A second/lingering instance would keep the singleton mutex and the packaged
    # .pyd files: the copy then failed with "file in use" and, later, the new
    # instance exited silently on the mutex.  Clear the field before installing.
    [void](Stop-RunningAssistants $root)
    if (-not (Wait-ForNoAssistants $root 10)) {
        Write-UpdateLog "WARNING: an assistant instance is still alive; the copy may fail with a locked file"
    }
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
    $backupRoot = Join-Path $root ("work\update-backup\" + (Get-Date -Format "yyyyMMdd_HHmmss"))
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
            # Snapshot first: a copy that dies half way must be reversible,
            # otherwise the restart launches a mixed tree and the new instance
            # dies at import (native fault, no log line).
            $step = "back up " + $root + " -> " + $backupRoot
            [void](Backup-ReplacedFiles $source $root $backupRoot)
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
        Write-UpdateLog "INSTALL FAILED: $installFailure"
        # Roll the snapshot back before restarting.  Restarting a half-applied
        # tree is what makes the fresh instance flash closed with no trace: a
        # new .py against an old .pyd dies natively, writing nothing.
        try {
            [void](Restore-Backup $backupRoot $root)
            $versionRestored = Read-InstalledVersion $root
            Write-UpdateLog ("install: rolled back to '" + $versionRestored +
                "'; the previous installation runs again")
        } catch {
            Write-UpdateLog ("RESTORE FAILED: " + $_.Exception.Message +
                " - the installation is inconsistent; reinstalling the package by hand is required")
        }
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
function Test-HelperElevated {
    # Windows refuses injected input across an integrity boundary (UIPI): a
    # medium-integrity assistant cannot type into an elevated game, and the only
    # symptom in the log is a perfectly normal attack beat with no attack in the
    # game (field report 2026-11-16: "the restarted instance right after the
    # update does not attack in either mode").  The launcher asks Windows for
    # "runas" for exactly this reason; this helper inherits whatever level the
    # assistant had, and it must not quietly lower that level when it restarts.
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Start-Assistant([string]$installRoot) {
    # Returns the pid of the instance that was started (0 when unknown).
    #
    # ORDER MATTERS and depends on this helper's own rights:
    #   * elevated helper -> start the packaged executable directly.  It inherits
    #     the elevated level, so no UAC prompt interrupts an automatic restart.
    #   * NOT elevated     -> start the operator's launcher first, because only
    #     that chain asks Windows for elevation (ShellExecute "runas").  A direct
    #     start here produced a medium-integrity assistant that could not inject
    #     keys into an elevated game - the 2026-11-16 "no attack after the
    #     update" report.
    # Then the packaged executable, then the batch file, as fallbacks.
    $elevated = Test-HelperElevated
    Write-UpdateLog ("restart: helper elevated=" + $elevated)
    if (-not $elevated) {
        # Ask Windows for the same level the operator's 启动助手.bat gets.  A
        # direct Start-Process here would produce a medium-integrity assistant
        # that cannot inject keys into an elevated game.
        $launcherFirst = Join-Path $installRoot "launch_assistant.vbs"
        if (Test-Path -LiteralPath $launcherFirst) {
            try {
                $process = Start-Process -FilePath "wscript.exe" -ArgumentList @("//nologo", $launcherFirst) `
                    -WorkingDirectory $installRoot -WindowStyle Hidden -PassThru -ErrorAction Stop
                Write-UpdateLog ("restart launched via launch_assistant.vbs (elevation requested) pid=" + $process.Id)
                return [int]$process.Id
            } catch {
                Write-UpdateLog "launcher (elevation) start failed: $($_.Exception.Message)"
            }
        }
    }
    foreach ($name in @("MoonEclipse.exe", "TodoHelper.exe", "MapleAssistant.exe")) {
        $candidate = Join-Path $installRoot $name
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $process = Start-Process -FilePath $candidate -WorkingDirectory $installRoot `
                -WindowStyle Hidden -PassThru -ErrorAction Stop
            Write-UpdateLog ("restart launched via " + $name + " pid=" + $process.Id)
            return [int]$process.Id
        } catch {
            Write-UpdateLog "$name start failed: $($_.Exception.Message)"
        }
    }
    $launcher = Join-Path $installRoot "launch_assistant.vbs"
    if (Test-Path -LiteralPath $launcher) {
        try {
            $process = Start-Process -FilePath "wscript.exe" -ArgumentList @("//nologo", $launcher) `
                -WorkingDirectory $installRoot -WindowStyle Hidden -PassThru -ErrorAction Stop
            Write-UpdateLog ("restart launched via launch_assistant.vbs pid=" + $process.Id)
            return [int]$process.Id
        } catch {
            Write-UpdateLog "launcher start failed: $($_.Exception.Message)"
        }
    }
    if (-not (Test-HelperElevated)) {
        Write-UpdateLog "WARNING: this helper is NOT elevated and the launcher chain is missing; the restarted assistant may be unable to inject keys into an elevated game (start it with 启动助手.bat)"
    }
    foreach ($name in @("start_assistant.bat", "启动助手.bat")) {
        $candidate = Join-Path $installRoot $name
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $process = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$candidate`"") `
                -WorkingDirectory $installRoot -WindowStyle Hidden -PassThru -ErrorAction Stop
            Write-UpdateLog ("restart launched via " + $name + " pid=" + $process.Id)
            return [int]$process.Id
        } catch {
            Write-UpdateLog "$name start failed: $($_.Exception.Message)"
        }
    }
    return 0
}

function Test-AssistantAlive([string]$installRoot, [int]$startedPid, [int]$seconds = 8) {
    # The old check scanned process NAMES once after 8 s and reported a false
    # negative even while the operator's own instance was alive (21:57:00 log:
    # "no MoonEclipse process" while pid 19496 had just started a helper).  Judge
    # by the process this helper created, and accept ANY assistant process that
    # started after the helper did.
    $deadline = [DateTime]::UtcNow.AddSeconds($seconds)
    do {
        if ($startedPid -gt 0) {
            $process = Get-Process -Id $startedPid -ErrorAction SilentlyContinue
            if ($process) { return $true }
        }
        foreach ($name in @("MoonEclipse", "TodoHelper", "MapleAssistant")) {
            $alive = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
            if ($alive.Count -gt 0) { return $true }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

$startedPid = Start-Assistant $root
if ($startedPid -eq 0) {
    Write-UpdateLog "FAILED: no launcher could be started"
    Show-OperatorMessage ("MoonEclipse was not restarted: no usable launcher was found in " + $root + "." + [Environment]::NewLine +
        "Start MoonEclipse.exe from that folder and send update-helper.log to the developer.")
} elseif (-not (Test-AssistantAlive $root $startedPid 8)) {
    # The single-instance mutex or a startup failure can kill the fresh instance
    # within a second, which used to be the end of it.  Retry through the same
    # launcher chain once, then judge again.
    Write-UpdateLog "WARNING: the restarted instance did not stay alive; retrying through the launcher once"
    Start-Sleep -Seconds 1
    $retryPid = Start-Assistant $root
    if (Test-AssistantAlive $root $retryPid 10) {
        Write-UpdateLog "restart confirmed after retry: pid=$retryPid"
        $startedPid = $retryPid
    } else {
        Write-UpdateLog ("WARNING: no MoonEclipse process after the retry (UAC declined, or the new install cannot start); " +
            "start it by hand with 启动助手.bat")
        Show-OperatorMessage ("MoonEclipse was not restarted automatically." + [Environment]::NewLine +
            "Start it with 启动助手.bat and send update-helper.log to the developer.")
    }
} else {
    Write-UpdateLog "restart confirmed: pid=$startedPid"
}
# The supervisor's own script is only swapped in now: while this run needed it,
# the file was open.  Done before the failure message so a successful update
# never leaves a *.ps1.new behind.
Complete-DeferredSupervisorUpdate
if ($installFailure) {
    Write-UpdateLog ("update summary: NOT installed (" + $installFailure +
        "); previous installation restored and restarted (pid=" + $startedPid + ")")
} else {
    Write-UpdateLog ("update summary: installed; running pid=" + $startedPid)
}
# One closing record plus an explicit exit: the supervisor must not linger in the
# task list (an idle PowerShell holding the install folder open is what blocked
# the next update).
Write-UpdateLog ("supervisor finished: mode=" + $Mode + " installFailure=" +
    $(if ($installFailure) { "yes" } else { "no" }) + " startedPid=" + $startedPid)
if ($supervisorLockOwned -and $supervisorLock) {
    try { $supervisorLock.ReleaseMutex() } catch { }
    try { $supervisorLock.Dispose() } catch { }
}
exit 0
