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

# The CT_IN 月卡 code is a per-customer credential, NOT package content.  The
# desktop receives it from the activation server in the device document and writes
# it into THIS file at runtime (local_lie_service.apply_online_card), so a bundled
# copy only leaked the operator's own card to every download.  The packaged copy is
# therefore scrubbed: the file stays (the service reads it at startup), but with an
# empty partner_license_code.  The repository copy is never touched here.
#
# Field behaviour that still holds: without a code in this file the vendor service
# cannot validate its 月卡, which is exactly why the desktop writes it before the
# service is started - the supervisor only starts it once a CT card arrived.
$settings = $null
try {
    # The 2026-11 vendor package (build_profile.client_version 1.0.0, exe
    # AppHostd83e.exe) writes this document in GBK/CP936: its placeholders are the
    # Chinese words for "your monthly card / your coordinate key", and 0xC4 is not
    # valid UTF-8.  Get-Content without -Encoding decodes with whatever code page
    # this machine happens to use, so read the bytes and try UTF-8 strictly first.
    $configBytes = [System.IO.File]::ReadAllBytes($config)
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        $configText = $strictUtf8.GetString($configBytes)
    } catch {
        $configText = [System.Text.Encoding]::Default.GetString($configBytes)
    }
    $settings = $configText | ConvertFrom-Json
} catch {
    throw "local lie service: data\ct_login_config.json is not valid JSON: $($_.Exception.Message)"
}
if ($null -eq $settings -or $settings -is [System.Array]) {
    throw "local lie service: data\ct_login_config.json must be a JSON object."
}
$configChanged = $false
$hasCard = $settings.PSObject.Properties.Name -contains "partner_license_code"
if ($hasCard -and [string]$settings.partner_license_code) {
    $settings.partner_license_code = ""
    $configChanged = $true
}
# A WS key that still carries the vendor's template text (the shipped placeholder
# is Chinese, which no real key contains) would fail the startup handshake, so it
# is cleared as well: the service then uses its own data\build_profile.json key.
$hasKey = $settings.PSObject.Properties.Name -contains "lie_product_key"
if ($hasKey -and ([string]$settings.lie_product_key) -match '[^\x20-\x7E]') {
    $settings.lie_product_key = ""
    $configChanged = $true
}
if ($configChanged) {
    # No BOM, and ConvertTo-Json escapes non-ASCII, so the result parses under any
    # code page - the vendor's locale reader and a strict UTF-8 reader agree.
    [System.IO.File]::WriteAllText(
        $config, ($settings | ConvertTo-Json -Depth 8),
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "local lie service: bundled ct_login_config.json scrubbed (partner card / placeholder key removed)" -ForegroundColor Cyan
} elseif ($hasCard) {
    Write-Host "local lie service: bundled ct_login_config.json already carries no partner card"
}

$mb = [math]::Round($bytes / 1MB, 1)
Write-Host "local lie service bundled: $copied file(s), $mb MB -> $target" -ForegroundColor Green
