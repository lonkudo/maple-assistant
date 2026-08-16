Option Explicit

Dim shellApp, pythonwPath, scriptPath, workingDirectory
pythonwPath = "C:\Users\SOTTES\Documents\Codex\2026-08-12\maplestory-worlds-automation\venv313\Scripts\python.exe"
scriptPath = "C:\Users\SOTTES\Documents\Codex\2026-08-12\maplestory-worlds-automation\visual_check.py"
workingDirectory = "C:\Users\SOTTES\Documents\Codex\2026-08-12\maplestory-worlds-automation"

Set shellApp = CreateObject("Shell.Application")
' runas verb = elevated, so the YOLO bot can capture and control the
' elevated MapleStory game window (UIPI would block it otherwise).
shellApp.ShellExecute pythonwPath, Chr(34) & scriptPath & Chr(34), workingDirectory, "runas", 0
