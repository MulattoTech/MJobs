param(
  [switch]$IncludeOllama,
  [ValidateSet('ollama/gpt-oss:20b','ollama/gemma4:26b')][string]$OllamaModel='ollama/gpt-oss:20b',
  [int]$Port=18704,
  [switch]$NoOpen
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
Set-Location $root
python -m tools.coordination.lane_coordination validate
if($LASTEXITCODE -ne 0){ throw 'Lane coordination validation failed.' }

& (Join-Path $root 'Start-CodexLane.ps1') -NoFollow -NoOpen -Port $Port
& (Join-Path $root 'Start-AntigravityLane.ps1') -NoFollow -NoOpen -Port $Port
if($IncludeOllama){
  & (Join-Path $root 'Start-OllamaLane.ps1') -Model $OllamaModel -NoFollow -NoOpen -Port $Port
}
$url="http://127.0.0.1:$Port/"
Write-Host "Parallel lanes are supervised at $url"
if(-not $NoOpen){ Start-Process $url }
