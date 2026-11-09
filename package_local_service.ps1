# Bundle the packaged CT partner API (the local lie-detection service) into a release.
#
# The assistant starts this service on 127.0.0.1:8765 when the local backend is
# selected (see local_lie_service.py) and finds it at
#   <install root>\CT_Lie3_KR_partner_api_DML\
# next to the program files.
#
# Only what the service needs to RUN is copied:
#   * the launcher chain (start_*.bat / *.vbs / the two readme files)
#   * the service binary (TaskBroker*.exe; a stale AppHost*.exe is dropped when a
#     TaskBroker is present, because the shipped launcher starts the TaskBroker)
#   * _internal\ (the frozen runtime, ONNX models, tcl/tk data)
#   * modes\tw_jingdian\ (the tracking profile + template pictures)
#   * data\ct_login_config.json and the small json/identity files
# Runtime junk is never shipped: data\logs, data\license (regenerated per
# machine), test_files\ (the service's own test media) and __pycache__.
#
# ASCII only: this file is read by Windows PowerShell, which decodes a BOM-less
# script with the ANSI code page - Chinese text here broke parsing before.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\package_local_service.ps1 `
#       -Destination <staged package folder OR Nuitka dist folder>

param(
    [Parameter(Mandatory = $true)] [string]$Destination,
    [string]$Source = ""
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
if (-not $Source) {
    $Source = Join-Path $repo "CT_Lie3_KR_partner_api_DML\CT_Lie3_KR_partner_api_DML"
}
$Source = [IO.Path]::GetFullPath($Source)
$destinationRoot = [IO.Path]::GetFullPath($Destination)

if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "local lie service folder is missing: $Source"
}
$launchers = @(
    "*.bat", "*.vbs"
) | ForEach-Object { @(Get-ChildItem -LiteralPath $Source -File -Filter $_ -ErrorAction SilentlyContinue) }
if (-not $launchers) {
    throw "local lie service folder has no launcher: $Source"
}
$serviceExes = @(Get-ChildItem -LiteralPath $Source -File -Filter "*.exe" -ErrorAction SilentlyContinue)
if (-not $serviceExes) {
    throw "local lie service folder has no executable: $Source"
}

$target = Join-Path $destinationRoot "CT_Lie3_KR_partner_api_DML"
if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}

# A stale AppHost*.exe is dead weight when the launcher starts a TaskBroker*.
$hasTaskBroker = @(Get-ChildItem -LiteralPath $Source -File -Filter "TaskBroker*.exe").Count -gt 0
$skipDirs = @("test_files", "logs", "license", "__pycache__")

$copied = 0
$bytes = 0L
Get-ChildItem -LiteralPath $Source -Recurse -Force | ForEach-Object {
    $relative = $_.FullName.Substring($Source.Length).TrimStart('\')
    if (-not $relative) { return }
    $segments = $relative.Split('\')
    foreach ($segment in $segments) {
        if ($skipDirs -contains $segment) { return }
    }
    if ($_.Name -like "*.pyc") { return }
    if ($hasTaskBroker -and $_.Name -like "AppHost*") { return }
    $out = Join-Path $target $relative
    if ($_.PSIsContainer) {
        New-Item -ItemType Directory -Path $out -Force | Out-Null
        return
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $out) -Force | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $out -Force
    $copied++
    $bytes += $_.Length
}

# The service needs its configuration to start; without it the process exits
# before listening on 8765 and the local backend would silently stay unavailable.
$config = Join-Path $target "data\ct_login_config.json"
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    throw "local lie service has no data\ct_login_config.json (card / WS key): $config"
}

# The CT_IN 月卡 is NOT device independent: the activated ticket lives in
# data\license (per machine, excluded from the bundle), so a fresh machine can
# only validate the card when its code is in data\ct_login_config.json.  Field
# report from another machine:
#   "CT_IN 月卡启动验票失败，同行服务未启动 ... 原因：未配置或未激活 CT_IN 月卡"
# The code is therefore copied from this machine's licence state into the BUNDLED
# config (the repository copy stays untouched).  The value is never printed.
$cardPath = Join-Path $Source "data\license\ct_partner_last_code.json"
if (Test-Path -LiteralPath $cardPath -PathType Leaf) {
    $card = ""
    try {
        $state = Get-Content -LiteralPath $cardPath -Raw | ConvertFrom-Json
        if ($state.PSObject.Properties.Name -contains "license_code") {
            $card = [string]$state.license_code
        }
    } catch {
        Write-Warning "local lie service: the saved CT_IN code could not be read: $($_.Exception.Message)"
    }
    if ($card) {
        $settings = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
        $current = ""
        if ($settings.PSObject.Properties.Name -contains "partner_license_code") {
            $current = [string]$settings.partner_license_code
        }
        if (-not $current) {
            $settings | Add-Member -NotePropertyName partner_license_code `
                -NotePropertyValue $card -Force
            $json = $settings | ConvertTo-Json -Depth 8
            # No BOM: the service parses this file with a strict UTF-8 reader.
            [System.IO.File]::WriteAllText(
                $config, $json, (New-Object System.Text.UTF8Encoding($false)))
            Write-Host "local lie service: CT_IN card code written into the bundled data\ct_login_config.json (value not printed)"
        } else {
            Write-Host "local lie service: data\ct_login_config.json already carries a CT_IN card code"
        }
    } else {
        Write-Warning "local lie service: no saved CT_IN code on this machine; the bundled service cannot validate the card elsewhere."
    }
} else {
    Write-Warning "local lie service: no data\license\ct_partner_last_code.json; set partner_license_code by hand before shipping."
}

$mb = [math]::Round($bytes / 1MB, 1)
Write-Host "local lie service bundled: $copied file(s), $mb MB -> $target" -ForegroundColor Green
