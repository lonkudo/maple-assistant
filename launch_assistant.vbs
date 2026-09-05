Option Explicit

Dim shellApp, files, root, pythonwPath, assistantPath, arguments, item, statusPath
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = root & "\.venv\Scripts\pythonw.exe"
' The probe records any exception that occurs before the normal application
' logging is available.  pythonw deliberately has no console, so without it
' a failed UAC launch looks like the BAT did nothing.
assistantPath = root & "\startup_probe.py"
statusPath = root & "\assistant-launch-status.log"
arguments = QuoteArgument(assistantPath)
For Each item In WScript.Arguments
    arguments = arguments & " " & QuoteArgument(CStr(item))
Next

Set shellApp = CreateObject("Shell.Application")
' Use the same integrity level as an elevated game. Without this, Windows
' will not deliver global low-level keyboard hooks for Ctrl hotkeys from the
' game window. Error handling below makes a declined/blocked UAC request
' visible instead of silently doing nothing.
WriteStatus "Launcher requested a hidden Python start."
On Error Resume Next
shellApp.ShellExecute pythonwPath, arguments, root, "runas", 0
If Err.Number <> 0 Then
    WriteStatus "Windows could not start the assistant: " & Err.Description
    MsgBox "Maple Assistant could not start. Open assistant-launch-status.log in this folder.", 16, "Maple Assistant"
End If
On Error GoTo 0

Function QuoteArgument(value)
    QuoteArgument = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function

Sub WriteStatus(message)
    Dim logFile
    Set logFile = files.OpenTextFile(statusPath, 8, True, 0)
    logFile.WriteLine Now & " " & message
    logFile.Close
End Sub
