Option Explicit
Dim shell, files, root, powershell, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%ProgramFiles%\PowerShell\7\pwsh.exe")
If Not files.FileExists(powershell) Then
  powershell = shell.ExpandEnvironmentStrings("%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe")
End If
command = """" & powershell & """ -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & root & "\Start-Remote-Status.ps1"""
If WScript.Arguments.Count = 1 Then
  command = command & " -OutputPath """ & Replace(WScript.Arguments(0), """", "") & """"
End If
WScript.Quit shell.Run(command, 0, True)
