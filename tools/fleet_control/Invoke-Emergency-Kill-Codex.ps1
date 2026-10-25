param(
 [Parameter(Mandatory=$true)]
 [string]$Confirm
)
$ErrorActionPreference='Stop'
if($Confirm -cne 'KILL ALL CODEX'){throw "Emergency action requires -Confirm 'KILL ALL CODEX'."}
$root='C:\Tools\AO-Fleet-Control'
$api='http://127.0.0.1:8787/api/master/emergency'
try{
 $result=Invoke-RestMethod -Uri $api -Method Post -ContentType 'application/json' -Headers @{'X-AO-Fleet'='1'} -Body '{"confirm":"KILL ALL CODEX"}' -TimeoutSec 5
 Write-Host "Emergency shutdown submitted through Fleet Control." -ForegroundColor Red
 Write-Host "Watch the Master Controls dashboard for zero-process verification."
 exit 0
}catch{
 Write-Warning 'Fleet Control local API unavailable; falling back to direct emergency shutdown.'
}
foreach($item in @('sustain-config.json','turbo-config.json')){
 $file=Join-Path $root $item
 if(Test-Path $file){
  try{$o=Get-Content $file -Raw|ConvertFrom-Json;$o.enabled=$false;($o|ConvertTo-Json -Depth 20)|Set-Content $file -Encoding UTF8}
  catch{Write-Warning "Could not persist disabled configuration: $item"}
 }
}
$marker=@{enabled=$true;triggeredAt=(Get-Date).ToUniversalTime().ToString('o');scope='all Codex processes on this Windows host';source='offline PowerShell launcher';explicitlyRequested=$true}
$marker|ConvertTo-Json|Set-Content (Join-Path $root 'emergency-latch.json') -Encoding UTF8
$script=Join-Path $root 'Emergency-Stop-Codex.ps1'
if(!(Test-Path $script)){throw 'Emergency process script missing: '+$script}
$pwsh='C:\Program Files\PowerShell\7\pwsh.exe'
if(!(Test-Path $pwsh)){throw 'PowerShell 7 is missing'}
$p=Start-Process -FilePath $pwsh -ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',('"' + $script + '"')) -WorkingDirectory $root -WindowStyle Hidden -PassThru
Write-Host "Emergency stop watchdog launched (PID $($p.Id))." -ForegroundColor Red
Write-Host 'Codex session processes are being terminated. Re-arm explicitly before starting AI again.'
