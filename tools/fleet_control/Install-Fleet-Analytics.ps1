$ErrorActionPreference='Stop'
$source=$PSScriptRoot
$root='C:\Tools\AO-Fleet-Control';$publish='C:\Tools\Dev\MTechLabs-Hub\site\mission-control\analytics'
New-Item -ItemType Directory -Force -Path "$root\telemetry",$publish|Out-Null
foreach($name in @('collect-analytics.py','Start-Fleet-Analytics.ps1')){if([IO.Path]::GetFullPath((Join-Path $source $name)) -ine [IO.Path]::GetFullPath((Join-Path $root $name))){Copy-Item (Join-Path $source $name) $root -Force}}
Get-ChildItem "$source\remote\public\analytics" -File|ForEach-Object{Copy-Item $_.FullName (Join-Path $publish $_.Name) -Force}
$existing=Get-ScheduledTask -TaskName 'MJobs Fleet - Dashboards Standard'
$account=$existing.Principal.UserId
$action=New-ScheduledTaskAction -Execute 'C:\Program Files\PowerShell\7\pwsh.exe' -Argument '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "C:\Tools\AO-Fleet-Control\Start-Fleet-Analytics.ps1"' -WorkingDirectory $root
$principal=New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $account
if(Get-ScheduledTask -TaskName 'MJobs Fleet - Analytics Standard' -ErrorAction SilentlyContinue){Stop-ScheduledTask -TaskName 'MJobs Fleet - Analytics Standard'}
Register-ScheduledTask -TaskName 'MJobs Fleet - Analytics Standard' -Action $action -Principal $principal -Settings $settings -Trigger $trigger -Description 'Read-only native fleet telemetry. No model prompts or agent control.' -Force|Out-Null
Start-ScheduledTask -TaskName 'MJobs Fleet - Analytics Standard'
[ordered]@{installed=$true;task='MJobs Fleet - Analytics Standard';privilege='Limited';publishesOnlySanitizedStatistics=$true;agentCommands=0}|ConvertTo-Json -Compress
