param([switch]$NoFollow,[switch]$NoOpen,[int]$Port=18704)
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'Start-AgentLane.ps1') -Lane antigravity -Model 'gemini-3.8-flash-medium' -Reasoning 'medium' -Port $Port -NoFollow:$NoFollow -NoOpen:$NoOpen
exit $LASTEXITCODE
