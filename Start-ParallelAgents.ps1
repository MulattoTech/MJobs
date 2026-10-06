param(
  [switch]$IncludeOllama,
  [ValidateSet('ollama/gpt-oss:20b','ollama/gemma4:26b')][string]$OllamaModel='ollama/gpt-oss:20b'
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
python -m tools.coordination.lane_coordination validate
if($LASTEXITCODE -ne 0){ throw 'Lane coordination validation failed.' }
& (Join-Path $root 'Start-CodexLane.ps1')
& (Join-Path $root 'Start-AntigravityLane.ps1')
if($IncludeOllama){ & (Join-Path $root 'Start-OllamaLane.ps1') -Model $OllamaModel }
Write-Host 'Parallel agent terminals launched. Each must claim a non-overlapping issue before mutation.'
