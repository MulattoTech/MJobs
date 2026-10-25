Option Explicit
Dim fileSystem, scriptRoot, shell, powerShellPath, command, resultCode
Set fileSystem = CreateObject("Scripting.FileSystemObject")
scriptRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)
powerShellPath = "C:\Program Files\PowerShell\7\pwsh.exe"
If Not fileSystem.FileExists(powerShellPath) Then WScript.Quit 2
If Not fileSystem.FileExists(fileSystem.BuildPath(scriptRoot, "Start-Orchestrator-Requested.ps1")) Then WScript.Quit 2
Set shell = CreateObject("WScript.Shell")
command = Chr(34) & powerShellPath & Chr(34) & " -NoLogo -NoProfile -NonInteractive -File " & Chr(34) & fileSystem.BuildPath(scriptRoot, "Start-Orchestrator-Requested.ps1") & Chr(34)
resultCode = shell.Run(command, 0, True)
WScript.Quit resultCode
