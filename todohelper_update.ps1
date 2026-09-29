<#
.SYNOPSIS
    Persistent TodoHelper restart and ZIP-update helper.

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
    [ValidateSet("source", "frozen")] [string]$PackageFormat = "source"
)

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($InstallRoot)
$log = Join-Path $root "update-helper.log"

function Write-UpdateLog([string]$message) {
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message" -Encoding UTF8
}

function Wait-ForTarget([int]$targetProcessId) {
    for ($i = 0; $i -lt 600; $i++) {
        # $PID is a PowerShell automatic, read-only variable.  Do not name a
        # function parameter $pid: parameter binding would fail before this
        # helper has a chance to wait for the assistant and restart it.
        if (-not (Get-Process -Id $targetProcessId -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 100
    }
    throw "Timed out waiting for TodoHelper to exit."
}

function Find-PackageRoot([string]$stage, [string]$format) {
    $candidates = @(Get-ChildItem -LiteralPath $stage -Recurse -File -Filter VERSION |
        ForEach-Object { $_.Directory } |
        Where-Object {
            if ($format -eq "frozen") {
                (Test-Path (Join-Path $_ "TodoHelper.exe")) -or (Test-Path (Join-Path $_ "MapleAssistant.exe"))
            } else {
                Test-Path (Join-Path $_ "assistant.py")
            }
        })
    if ($candidates.Count -ne 1) { throw "Update ZIP has no unambiguous compatible TodoHelper package." }
    return $candidates[0].FullName
}

function Copy-Package([string]$source, [string]$destination) {
    Get-ChildItem -LiteralPath $source -Recurse -Force | ForEach-Object {
        $relative = $_.FullName.Substring($source.Length).TrimStart('\')
        if (-not $relative -or $relative -match '^(\.git|\.venv|work|node_modules)(\\|$)') { return }
        if ($relative -ieq "user_config.json") { return }
        $target = Join-Path $destination $relative
        if ($_.PSIsContainer) {
            New-Item -ItemType Directory -Path $target -Force | Out-Null
        } else {
            New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        }
    }
}

try {
    Write-UpdateLog "helper started mode=$Mode pid=$TargetPid"
    Wait-ForTarget $TargetPid
    if ($Mode -eq "Update") {
        if (-not (Test-Path -LiteralPath $PackagePath)) { throw "Update ZIP is missing: $PackagePath" }
        $stage = Join-Path ([IO.Path]::GetTempPath()) ("todohelper-update-" + [guid]::NewGuid().ToString("N"))
        try {
            Expand-Archive -LiteralPath $PackagePath -DestinationPath $stage -Force
            $source = Find-PackageRoot $stage $PackageFormat
            Copy-Package $source $root
            Remove-Item -LiteralPath $PackagePath -Force -ErrorAction Stop
            Write-UpdateLog "update applied and consumed ZIP=$PackagePath"
        } finally {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $launcher = Join-Path $root "launch_assistant.vbs"
    if (-not (Test-Path -LiteralPath $launcher)) { throw "Launcher missing after update." }
    Start-Process -FilePath "wscript.exe" -ArgumentList @("//nologo", $launcher) -WorkingDirectory $root -WindowStyle Hidden
    Write-UpdateLog "restart launched"
} catch {
    Write-UpdateLog "FAILED: $($_.Exception.Message)"
}
