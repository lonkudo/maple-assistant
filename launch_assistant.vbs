Option Explicit

Dim shellApp, files, root, pythonwPath, assistantPath, arguments, item
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = root & "\.venv\Scripts\pythonw.exe"
assistantPath = root & "\assistant.py"
arguments = QuoteArgument(assistantPath)
For Each item In WScript.Arguments
    arguments = arguments & " " & QuoteArgument(CStr(item))
Next

Set shellApp = CreateObject("Shell.Application")
' runas shows one UAC prompt; window style 0 keeps the Python console hidden.
shellApp.ShellExecute pythonwPath, arguments, root, "runas", 0

Function QuoteArgument(value)
    QuoteArgument = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
