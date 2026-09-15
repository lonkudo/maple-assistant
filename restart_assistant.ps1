$ErrorActionPreference = 'Stop'

# Development restart helper (excluded from releases).  It restarts the local
# checkout through the renamed interpreter the launcher uses, so the process
# name stays "todo_helper.exe".
$root = [IO.Path]::GetFullPath(
    'C:\Users\SOTTES\Documents\Codex\2026-08-12\skill-creator-c-users-sottes-codex-2'
)
$assistantPath = [IO.Path]::GetFullPath((Join-Path $root 'assistant.py'))
$pythonwPath = [IO.Path]::GetFullPath((Join-Path $root '.venv\Scripts\pythonw.exe'))
$exePath = [IO.Path]::GetFullPath((Join-Path $root '.venv\Scripts\todo_helper.exe'))
$workingDirectory = [IO.Path]::GetDirectoryName($assistantPath)

if (-not (Test-Path -LiteralPath $assistantPath -PathType Leaf)) {
    throw "Assistant entry point is missing: $assistantPath"
}
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
        throw "Python windowed executable is missing: $pythonwPath"
    }
    Copy-Item -LiteralPath $pythonwPath -Destination $exePath -Force
}

function Get-AssistantProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'todo_helper.exe' -and
        $_.ExecutablePath -eq $exePath -and
        $_.CommandLine -like "*$workingDirectory*"
    }
}

foreach ($target in @(Get-AssistantProcesses)) {
    if ([string]::IsNullOrWhiteSpace($target.CommandLine)) {
        throw "Refusing to stop process $($target.ProcessId) without a verified command line"
    }
    Stop-Process -Id ([int]$target.ProcessId) -ErrorAction Stop
}

$deadline = (Get-Date).AddSeconds(5)
$remaining = @(Get-AssistantProcesses)
while ($remaining.Count -ne 0 -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 100
    $remaining = @(Get-AssistantProcesses)
}

if ($remaining.Count -ne 0) {
    throw 'The previous todo_helper instance did not stop cleanly'
}

Start-Process -FilePath $exePath `
    -ArgumentList ('"' + $assistantPath + '"') `
    -WorkingDirectory $workingDirectory `
    -WindowStyle Hidden
