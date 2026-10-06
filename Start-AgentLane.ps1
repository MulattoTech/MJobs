param(
  [Parameter(Mandatory=$true)]
  [ValidateSet('codex','antigravity','ollama')]
  [string]$Lane,
  [string]$Model,
  [string]$Reasoning,
  [int]$Port=18704,
  [switch]$NoFollow,
  [switch]$NoOpen
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$python=Join-Path $root '.venv\Scripts\python.exe'
if(-not (Test-Path $python)){ throw "MJobs virtualenv not found: $python" }
$base="http://127.0.0.1:$Port"

function Test-Dashboard {
  try {
    $null=Invoke-RestMethod -Uri "$base/api/state" -TimeoutSec 1
    return $true
  } catch { return $false }
}

if(-not (Test-Dashboard)){
  $args=@('-m','tools.agent_dashboard.server','--root',$root,'--port',[string]$Port,'--no-open')
  Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $root -WindowStyle Hidden
  $ready=$false
  for($i=0; $i -lt 30; $i++){
    Start-Sleep -Milliseconds 200
    if(Test-Dashboard){ $ready=$true; break }
  }
  if(-not $ready){ throw "Agent dashboard did not become ready on $base" }
}

$patch=@{}
if($Model){ $patch.model=$Model }
if($Reasoning){ $patch.reasoning=$Reasoning }
if($patch.Count -gt 0){
  $json=$patch | ConvertTo-Json -Compress
  Invoke-RestMethod -Method Patch -Uri "$base/api/lanes/$Lane/config" -ContentType 'application/json' -Body $json | Out-Null
}
$state=Invoke-RestMethod -Method Post -Uri "$base/api/lanes/$Lane/start" -ContentType 'application/json' -Body '{}'
Write-Host "[$Lane] supervisor status=$($state.status) PID=$($state.pid)"
Write-Host "[$Lane] dashboard: $base/"
if(-not $NoOpen){ Start-Process "$base/" }
if($NoFollow){ exit 0 }

$log=Join-Path $root "outputs\agent-dashboard\$Lane.log"
while(-not (Test-Path $log)){ Start-Sleep -Milliseconds 200 }
Write-Host "[$Lane] following $log (Ctrl+C stops log following; the supervised agent continues)"
Get-Content -LiteralPath $log -Wait
