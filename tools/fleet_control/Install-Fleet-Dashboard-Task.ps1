param(
 [string]$Root='C:\Tools\AO-Fleet-Control',
 [string]$TaskName='MJobs Fleet - Dashboards Standard',
 [switch]$RunNow
)
$ErrorActionPreference='Stop'
$wrapper=Join-Path $Root 'Start-Fleet-Control.vbs'
if(!(Test-Path -LiteralPath $wrapper -PathType Leaf)){throw "Dashboard wrapper missing: $wrapper"}
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$action=New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR 'System32\wscript.exe') -Argument ('//B //NoLogo "'+$wrapper+'"') -WorkingDirectory $Root
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $user
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1)
# Only the dashboards start at sign-in. Fleet restoration and task dispatch
# remain explicit protected actions in the board.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Start the MJobs dashboards invisibly in the signed-in standard user session. Does not start agents.' -Force | Out-Null
if($RunNow){Start-ScheduledTask -TaskName $TaskName}
[pscustomobject]@{taskName=$TaskName;user=$user;runLevel='Limited';hidden=$true;startsAgents=$false;started=[bool]$RunNow}|ConvertTo-Json -Compress
