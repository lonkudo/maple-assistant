Option Explicit

Dim shell, files, root, pythonw, app
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
pythonw = root & "\.venv\Scripts\pythonw.exe"
app = root & "\app.py"

If files.FileExists(pythonw) And files.FileExists(app) Then
    shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & app & Chr(34), 0, False
Else
    MsgBox "BOSS Tracker is not installed. Run the installer first.", 48, "BOSS Tracker"
End If
