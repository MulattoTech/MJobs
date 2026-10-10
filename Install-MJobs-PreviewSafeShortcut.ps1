# Install / upgrade the user's MJobs Mission Control desktop shortcut safely.
[CmdletBinding()]
param(
  [string]$RepoRoot,
  [ValidateRange(1024,65535)][int]$Port=18726,
  [string]$ShortcutName='MJobs Mission Control'
)
$ErrorActionPreference='Stop'
$launcher=Join-Path $PSScriptRoot 'Start-MJobs-PreviewSafe.ps1'
if(-not(Test-Path -LiteralPath $launcher)){throw "Missing versioned launcher: $launcher"}
if(-not $RepoRoot){$RepoRoot=Join-Path (Split-Path -Parent $PSScriptRoot) 'MJobs'}
$RepoRoot=[IO.Path]::GetFullPath($RepoRoot)
if(-not(Test-Path -LiteralPath (Join-Path $RepoRoot 'AGENTS.md'))){
  throw "MJobs repository is missing: $RepoRoot"
}
if($ShortcutName.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0){
  throw 'Invalid shortcut name'
}
$desktop=[Environment]::GetFolderPath('Desktop')
if(-not(Test-Path -LiteralPath $desktop)){throw "Desktop path unavailable: $desktop"}
$link=Join-Path $desktop ($ShortcutName+'.lnk')
if(Test-Path -LiteralPath $link){
  $backupDir=Join-Path $env:LOCALAPPDATA 'MJobs\backups'
  New-Item -ItemType Directory -Path $backupDir -Force|Out-Null
  $backup=Join-Path $backupDir ($ShortcutName+'.before-preview-fix-20261010.lnk')
  if(-not(Test-Path -LiteralPath $backup)){
    Copy-Item -LiteralPath $link -Destination $backup -ErrorAction Stop
    Write-Output ('Saved previous shortcut: '+$backup)
  }
}
$target=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shell=New-Object -ComObject WScript.Shell
$shortcut=$shell.CreateShortcut($link)
$shortcut.TargetPath=$target
$shortcut.Arguments='-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "'+$launcher+'" -RepoRoot "'+$RepoRoot+'" -Port '+$Port
$shortcut.WorkingDirectory=$PSScriptRoot
$shortcut.Description='MJobs Mission Control - exact prompt preview, mandatory approval, isolated coding CLIs'
$shortcut.IconLocation=(Join-Path $env:SystemRoot 'System32\shell32.dll')+',43'
$shortcut.Save()
$readback=$shell.CreateShortcut($link)
if($readback.TargetPath -ne $target -or
   -not $readback.Arguments.Contains($launcher) -or
   -not $readback.Arguments.Contains($RepoRoot)){
  throw "Shortcut verification failed: $link"
}
Write-Output ('VERIFIED_SHORTCUT='+$link)
Write-Output ('TARGET='+$readback.TargetPath)
Write-Output ('ARGUMENTS='+$readback.Arguments)
