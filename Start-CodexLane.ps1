param([switch]$NoFollow,[switch]$NoOpen,[int]$Port=18704)
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'Start-AgentLane.ps1') -Lane codex -Model 'gpt-6-luna' -Reasoning 'high' -Port $Port -NoFollow:$NoFollow -NoOpen:$NoOpen
exit $LASTEXITCODE
