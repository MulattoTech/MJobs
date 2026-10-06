param(
  [int]$Port=18704,
  [switch]$NoOpen
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$python=Join-Path $root '.venv\Scripts\python.exe'
$base="http://127.0.0.1:$Port"
try {
  $null=Invoke-RestMethod -Uri "$base/api/state" -TimeoutSec 1
  Write-Host "Agent dashboard already running at $base/"
  if(-not $NoOpen){ Start-Process "$base/" }
  exit 0
} catch {}
$args=@('-m','tools.agent_dashboard.server','--root',$root,'--port',[string]$Port)
if($NoOpen){ $args += '--no-open' }
Set-Location $root
& $python @args
exit $LASTEXITCODE
