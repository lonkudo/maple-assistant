<#
.SYNOPSIS
    Build the separately named MapleAssistant-vnt-1.0.0 package.

.DESCRIPTION
    This script never edits the normal working source. It first builds a
    normal release into an isolated output folder, then removes trade-only
    wiring from that copied folder before creating the no-trade ZIP.
#>

param(
    [string]$Version
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($Version)) {
    # Default to the current normal-release version, so the no-trade package
    # always clearly identifies the exact source it was built from.
    $Version = (Get-Content -LiteralPath (Join-Path $root "VERSION") -Raw).Trim()
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version must be a normal release number like 1.0.0."
}
$releaseRoot = Join-Path $root "release"
$packageName = "MapleAssistant-vnt-$Version"
$outRelative = "release\$packageName"
$out = Join-Path $root $outRelative
$zip = Join-Path $releaseRoot "$packageName.zip"

function Replace-RequiredText {
    param(
        [string]$Path,
        [string]$Old,
        [string]$New = ""
    )
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $text = [regex]::Replace($text, "`r?`n", "`r`n")
    if (-not $text.Contains($Old)) {
        throw "Expected no-trade release text was not found in $Path"
    }
    $updated = $text.Replace($Old, $New)
    [System.IO.File]::WriteAllText(
        $Path, $updated, (New-Object System.Text.UTF8Encoding $false)
    )
}

function Replace-RequiredRegex {
    param(
        [string]$Path,
        [string]$Pattern,
        [string]$Replacement
    )
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $text = [regex]::Replace($text, "`r?`n", "`r`n")
    $updated = [regex]::Replace($text, $Pattern, $Replacement)
    if ($updated -eq $text) {
        throw "Expected no-trade release section was not found in $Path"
    }
    [System.IO.File]::WriteAllText(
        $Path, $updated, (New-Object System.Text.UTF8Encoding $false)
    )
}

Write-Host "== no-trade build: stage current source ==" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $root "build_release.ps1") `
    -OutDir $outRelative -Version $Version
if ($LASTEXITCODE -ne 0) {
    throw "Base release staging failed."
}

# The normal source is untouched from here onward: every edit targets $out.
[System.IO.File]::WriteAllText(
    (Join-Path $out "VERSION"), "$Version`n", [System.Text.Encoding]::ASCII
)

$assistant = Join-Path $out "assistant.py"
Replace-RequiredText $assistant "    from trade_worker import TradeWorker`r`n"
Replace-RequiredText $assistant "    trade_capture_active = threading.Event()`r`n"
Replace-RequiredText $assistant "    trade_frames: queue.Queue = queue.Queue(maxsize=1)`r`n"
Replace-RequiredText $assistant "        movement_frames, status_frames, character_frames, lie_detector_frames,`r`n        trade_frames,`r`n" "        movement_frames, status_frames, character_frames, lie_detector_frames,`r`n"
Replace-RequiredText $assistant "            game_focused, patrol_preparing, trade_capture_active`r`n" "            game_focused, patrol_preparing`r`n"
Replace-RequiredRegex $assistant "(?s)`r?`n    trade_worker = TradeWorker\(.*?`r?`n    \)`r?`n    movement_worker = MovementWorker\(" "`r`n    movement_worker = MovementWorker("
Replace-RequiredText $assistant "        trade_worker,`r`n"
Replace-RequiredText $assistant "            trade_worker=trade_worker,`r`n"

$ui = Join-Path $out "ui_worker.py"
Replace-RequiredText $ui "        trade_worker: Any = None,`r`n"
Replace-RequiredText $ui "        # Isolated Ctrl+Q/Ctrl+W trade workflow.  It uses the existing game`r`n        # capture worker only while checking for a trader.`r`n        self.trade_worker = trade_worker`r`n"
Replace-RequiredRegex $ui '(?s)\r?\n                elif action\.startswith\("trade:"\):.*?\r?\n                elif action\.startswith\("record:"\):' "`r`n                elif action.startswith(`"record:`"):"
Replace-RequiredText $ui "            # Trade controls are intentionally not public help bindings.`r`n            if action.startswith(`"trade:`"):`r`n                continue`r`n"
Replace-RequiredText $ui "            # A trade overlay may temporarily own another Tk interpreter;`r`n" "            # Another optional Tk interpreter may temporarily exist;`r`n"

$hotkey = Join-Path $out "hotkey.json"
$hotkeyData = Get-Content -LiteralPath $hotkey -Raw | ConvertFrom-Json
$hotkeyData.bindings = @($hotkeyData.bindings | Where-Object {
    -not [string]$_.action -like "trade:*"
})
$hotkeyData | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $hotkey -Encoding UTF8

$hotkeyWorker = Join-Path $out "hotkey_worker.py"
Replace-RequiredText $hotkeyWorker ('            or action == "trade:invite"' + "`r`n") ""
Replace-RequiredText $hotkeyWorker "        # Ctrl+Q is a start/cancel toggle.  It must bypass the normal two-`r`n        # second action cooldown so a second physical press can immediately`r`n        # cancel a pending trade wait.  MOD_NOREPEAT/the hook key-up state`r`n        # still prevent a held chord from firing repeatedly.`r`n" ""

$tradeModule = Join-Path $out "trade_worker.py"
if (Test-Path -LiteralPath $tradeModule) {
    Remove-Item -LiteralPath $tradeModule -Force
}
$scriptCopy = Join-Path $out "release_no_trade.ps1"
if (Test-Path -LiteralPath $scriptCopy) {
    Remove-Item -LiteralPath $scriptCopy -Force
}

Write-Host "== no-trade build: verify staged package ==" -ForegroundColor Cyan
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
Push-Location $out
try {
    & $python -m py_compile assistant.py ui_worker.py hotkey_worker.py
    if ($LASTEXITCODE -ne 0) { throw "No-trade package Python verification failed." }
    $references = rg -n -i "trade" assistant.py ui_worker.py hotkey_worker.py hotkey.json
    if ($LASTEXITCODE -eq 0 -and $references) {
        throw "No-trade package still contains trade wiring: $references"
    }
    if ($LASTEXITCODE -gt 1) { throw "No-trade package reference check failed." }
} finally {
    Pop-Location
}

if (Test-Path -LiteralPath $zip) {
    Remove-Item -LiteralPath $zip -Force
}
Compress-Archive -Path $out -DestinationPath $zip -CompressionLevel Optimal -Force
$zipInfo = Get-Item -LiteralPath $zip
Write-Host "no-trade release ready: $($zipInfo.FullName)" -ForegroundColor Green
