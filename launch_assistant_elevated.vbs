Option Explicit

Dim shellApp, pythonwPath, assistantPath, workingDirectory
pythonwPath = "C:\Users\SOTTES\AppData\Local\Programs\Python\Python310\pythonw.exe"
assistantPath = "C:\Users\SOTTES\Documents\Codex\2026-08-12\skill-creator-c-users-sottes-codex-2\assistant.py"
workingDirectory = "C:\Users\SOTTES\Documents\Codex\2026-08-12\skill-creator-c-users-sottes-codex-2"

Set shellApp = CreateObject("Shell.Application")
' The runas verb displays the standard UAC confirmation and launches pythonw,
' so no command window remains open behind the assistant UI.
shellApp.ShellExecute pythonwPath, Chr(34) & assistantPath & Chr(34), workingDirectory, "runas", 0
