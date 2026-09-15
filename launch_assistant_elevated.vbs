Option Explicit

Dim shellApp, files, pythonwPath, exePath, assistantPath, workingDirectory, debugFlags
Set files = CreateObject("Scripting.FileSystemObject")
pythonwPath = "C:\Users\SOTTES\Documents\Codex\2026-08-12\skill-creator-c-users-sottes-codex-2\.venv\Scripts\pythonw.exe"
exePath = "C:\Users\SOTTES\Documents\Codex\2026-08-12\skill-creator-c-users-sottes-codex-2\.venv\Scripts\todo_helper.exe"
assistantPath = "C:\Users\SOTTES\Documents\Codex\2026-08-12\skill-creator-c-users-sottes-codex-2\assistant.py"
workingDirectory = "C:\Users\SOTTES\Documents\Codex\2026-08-12\skill-creator-c-users-sottes-codex-2"
' Debug mode: save screenshots (with ROI rectangles drawn) under work\debug.
debugFlags = " --debug-dir work\debug --debug-capture-regions"

' Launch through a renamed interpreter so the running process is
' "todo_helper.exe" (and the game sees that name) instead of "pythonw.exe".
On Error Resume Next
If Not files.FileExists(exePath) Then files.CopyFile pythonwPath, exePath, True
If Not files.FileExists(exePath) Then exePath = pythonwPath
On Error GoTo 0

Set shellApp = CreateObject("Shell.Application")
' The runas verb displays the standard UAC confirmation and launches pythonw,
' so no command window remains open behind the assistant UI.
shellApp.ShellExecute exePath, Chr(34) & assistantPath & Chr(34) & debugFlags, workingDirectory, "runas", 0
