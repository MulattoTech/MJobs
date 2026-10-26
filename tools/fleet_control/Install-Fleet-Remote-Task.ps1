param(
 [string]$Root='C:\Tools\AO-Fleet-Control',
 [string]$OutputPath='C:\Tools\Dev\MTechLabs-Hub\site\mission-control',
 [string]$TaskName='MJobs Fleet - Remote Status Standard',
 [switch]$RunNow
)
$ErrorActionPreference='Stop'
$wrapper=Join-Path $Root 'Start-Remote-Status.vbs'
if(!(Test-Path -LiteralPath $wrapper -PathType Leaf)){throw "Remote status wrapper missing: $wrapper"}
if(!(Test-Path -LiteralPath (Join-Path $OutputPath 'index.html') -PathType Leaf)){throw 'Install the remote status page before creating its task.'}
if($OutputPath.Contains('"')){throw 'The output directory cannot contain quotes.'}
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$action=New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR 'System32\wscript.exe') -Argument ('//B //NoLogo "'+$wrapper+'" "'+$OutputPath+'"') -WorkingDirectory $Root
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $user
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Export allowlisted, read-only fleet status to the existing protected M-TechLabs hub. Starts no agents and opens no listener.' -Force | Out-Null
if($RunNow){Start-ScheduledTask -TaskName $TaskName}
[pscustomobject]@{taskName=$TaskName;user=$user;runLevel='Limited';hidden=$true;startsAgents=$false;started=[bool]$RunNow}|ConvertTo-Json -Compress
