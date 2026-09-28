param(
    [Parameter(Mandatory = $true)]
    [string]$Root
)

$ErrorActionPreference = "Stop"
$rootPath = [System.IO.Path]::GetFullPath($Root)
$venvScripts = Join-Path $rootPath ".venv\Scripts"
$pythonwPath = Join-Path $venvScripts "pythonw.exe"
$assistantExePath = Join-Path $venvScripts "TodoHelper.exe"
$startupPath = Join-Path $rootPath "startup_probe.py"

if (-not (Test-Path -LiteralPath $pythonwPath)) {
    throw "Virtual environment missing. Run the setup first."
}

# The launcher is deliberately a restart command: an old assistant is never
# left holding the singleton mutex while the new one is starting.  The renamed
# interpreter is the normal production process name; the Python fallback is
# restricted to this installation directory so unrelated Python programs stay
# untouched.
$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and (
        $_.Name -ieq "TodoHelper.exe" -or
        (( $_.Name -ieq "pythonw.exe" -or $_.Name -ieq "python.exe" ) -and
         $_.CommandLine -like "*startup_probe.py*" -and
         $_.CommandLine -like "*$rootPath*")
    )
}
foreach ($target in $targets) {
    Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
}

# Wait briefly for Windows to release the old mutex before launching its
# replacement.  This remains hidden because this script is invoked by WSH.
$deadline = [DateTime]::UtcNow.AddSeconds(3)
do {
    Start-Sleep -Milliseconds 100
    $remaining = Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.Name -ieq "TodoHelper.exe"
    }
} while ($remaining -and [DateTime]::UtcNow -lt $deadline)

if (-not (Test-Path -LiteralPath $assistantExePath)) {
    Copy-Item -LiteralPath $pythonwPath -Destination $assistantExePath -Force
}
if (-not (Test-Path -LiteralPath $assistantExePath)) {
    $assistantExePath = $pythonwPath
}

Start-Process -FilePath $assistantExePath -ArgumentList @($startupPath) -WorkingDirectory $rootPath -WindowStyle Hidden
