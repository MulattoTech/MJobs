$ErrorActionPreference='Stop'
$connections=@(Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue)
if(!$connections.Count){Write-Host 'Dashboard is already stopped';exit 0}
$pids=@($connections|Select-Object -ExpandProperty OwningProcess -Unique)
foreach($id in $pids){
 $p=Get-CimInstance Win32_Process -Filter "ProcessId = $id"
 if(!$p -or $p.Name -ne 'node.exe'){throw "PID $id is not the expected node.exe process; stopping refused"}
 if($p.CommandLine){
  if($p.CommandLine -notlike '*AO-Fleet-Control*server.js*'){throw "PID $id command line is not Fleet Control; stopping refused"}
 }else{
  try{$health=Invoke-RestMethod 'http://127.0.0.1:8787/api/health' -TimeoutSec 2}catch{throw "Cannot verify dashboard listener PID $id; stopping refused"}
  if(!$health.ok -or $health.port -ne 8787 -or $health.host -ne '127.0.0.1'){throw "Health check does not identify Fleet Control; stopping refused"}
 }
 Stop-Process -Id $id -Force
 Write-Host "Stopped dashboard server PID $id"
}
