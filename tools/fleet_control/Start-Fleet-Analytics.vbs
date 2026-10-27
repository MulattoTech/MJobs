Option Explicit
Dim files, shell, root, powershell, command, code
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = files.GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%ProgramFiles%") & "\PowerShell\7\pwsh.exe"
If Not files.FileExists(powershell) Then WScript.Quit 2
command = """" & powershell & """ -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File """ & root & "\Start-Fleet-Analytics.ps1"""
code = shell.Run(command, 0, True)
WScript.Quit code
