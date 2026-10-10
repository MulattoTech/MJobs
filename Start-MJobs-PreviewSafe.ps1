# Start reviewed Mission Control from this versioned code checkout against REAL MJobs.
# NEVER starts a coding CLI or takes over an occupied process.
[CmdletBinding()]
param(
  [string]$RepoRoot,
  [ValidateRange(1024,65535)][int]$Port=18726,
  [switch]$NoBrowser
)
$ErrorActionPreference='Stop'
$codeRoot=$PSScriptRoot
if(-not $RepoRoot){$RepoRoot=Join-Path (Split-Path -Parent $codeRoot) 'MJobs'}
$RepoRoot=[IO.Path]::GetFullPath($RepoRoot)
if(-not(Test-Path -LiteralPath (Join-Path $RepoRoot 'AGENTS.md'))){
  throw "MJobs workspace is missing or invalid: $RepoRoot"
}
$python=Join-Path $RepoRoot '.venv\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $python)){
  throw "Expected MJobs virtual environment: $python"
}
$base="http://127.0.0.1:$Port"
function Get-CurrentHealth {
  try{return Invoke-RestMethod -Uri "$base/api/launcher/health" -TimeoutSec 3 -ErrorAction Stop}
  catch{return $null}
}
$current=Get-CurrentHealth
$listener=Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if($listener){
  if($current -and $current.service -eq 'mjobs-mission-control' -and
     $current.version -ge 6 -and $current.prompt_preview -eq $true -and
     $current.approval_required -eq $true -and $current.history_available -eq $true -and
     $current.visible_terminal -eq $true -and $current.subagent_controls -eq $true -and
     $current.prompt_via_stdin -eq $true -and
     $current.console_output_binding -eq 'CONOUT$' -and
     [IO.Path]::GetFullPath([string]$current.root) -eq $RepoRoot){
    Write-Host "Reusing verified safe Mission Control at $base/" -ForegroundColor Cyan
  }else{
    throw ("Port $Port is occupied by an older or unrelated dashboard. " +
           "Check all managed CLI sessions before stopping the older dashboard; " +
           "this launcher will NEVER terminate or take over its process.")
  }
}else{
  $logs=Join-Path $env:LOCALAPPDATA 'MJobs\MissionControlLaunch'
  New-Item -ItemType Directory -Force -Path $logs|Out-Null
  $stdout=Join-Path $logs 'preview-safe-stdout.log'
  $stderr=Join-Path $logs 'preview-safe-stderr.log'
  $args=@('-m','tools.agent_dashboard.server','--root',('"' + $RepoRoot + '"'),
          '--port',[string]$Port,'--mission-control','--no-open')
  $proc=Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $codeRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
  $ready=$false
  for($i=0;$i -lt 64;$i++){
    Start-Sleep -Milliseconds 240
    if($proc.HasExited){break}
    $health=Get-CurrentHealth
    if($health -and $health.service -eq 'mjobs-mission-control' -and
       $health.version -ge 6 -and $health.prompt_preview -eq $true -and
       $health.approval_required -eq $true -and $health.history_available -eq $true -and
       $health.visible_terminal -eq $true -and $health.subagent_controls -eq $true -and
       $health.prompt_via_stdin -eq $true -and
       $health.console_output_binding -eq 'CONOUT$' -and
       [IO.Path]::GetFullPath([string]$health.root) -eq $RepoRoot){
      $ready=$true
      break
    }
  }
  if(-not $ready){
    throw "Mission Control v3 did not become ready. Check $stderr. No coding CLI was launched."
  }
  Write-Host "Verified safe Mission Control started at $base/" -ForegroundColor Cyan
}
if(-not $NoBrowser){Start-Process "$base/"|Out-Null}
