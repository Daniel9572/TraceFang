' Native application window; no persistent console window.
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = files.GetParentFolderName(WScript.ScriptFullName)
script = files.BuildPath(root, "scripts\application.ps1")
shell.Run "powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File """ & script & """", 0, False
