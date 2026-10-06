param(
  [ValidateSet('ollama/gpt-oss:20b','ollama/gemma4:26b')][string]$Model='ollama/gpt-oss:20b',
  [switch]$NoFollow,
  [switch]$NoOpen,
  [int]$Port=18704
)
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'Start-AgentLane.ps1') -Lane ollama -Model $Model -Reasoning 'high' -Port $Port -NoFollow:$NoFollow -NoOpen:$NoOpen
exit $LASTEXITCODE
