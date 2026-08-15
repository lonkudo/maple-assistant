$ErrorActionPreference = 'Stop'

$assistantPath = [IO.Path]::GetFullPath(
    'C:\Users\SOTTES\Documents\Codex\2026-08-12\skill-creator-c-users-sottes-codex-2\assistant.py'
)
$pythonwPath = [IO.Path]::GetFullPath(
    'C:\Users\SOTTES\AppData\Local\Programs\Python\Python310\pythonw.exe'
)
$workingDirectory = [IO.Path]::GetDirectoryName($assistantPath)

if (-not (Test-Path -LiteralPath $assistantPath -PathType Leaf)) {
    throw "Assistant entry point is missing: $assistantPath"
}
if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
    throw "Python windowed executable is missing: $pythonwPath"
}

$targets = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'pythonw.exe' -and
        $_.ExecutablePath -eq $pythonwPath -and
        $_.CommandLine -like "*$assistantPath*"
    }
)

foreach ($target in $targets) {
    if ([string]::IsNullOrWhiteSpace($target.CommandLine)) {
        throw "Refusing to stop process $($target.ProcessId) without a verified command line"
    }
    Stop-Process -Id ([int]$target.ProcessId) -ErrorAction Stop
}

$deadline = (Get-Date).AddSeconds(5)
do {
    $remaining = @(
        Get-CimInstance Win32_Process | Where-Object {
            $_.Name -eq 'pythonw.exe' -and
            $_.ExecutablePath -eq $pythonwPath -and
            $_.CommandLine -like "*$assistantPath*"
        }
    )
    if ($remaining.Count -eq 0) { break }
    Start-Sleep -Milliseconds 100
} while ((Get-Date) -lt $deadline)

if ($remaining.Count -ne 0) {
    throw 'The previous Maple Assistant instance did not stop cleanly'
}

Start-Process -FilePath $pythonwPath `
    -ArgumentList ('"' + $assistantPath + '"') `
    -WorkingDirectory $workingDirectory `
    -WindowStyle Hidden
