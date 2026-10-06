param(
  [switch]$IncludeOllama,
  [ValidateSet('ollama/gpt-oss:20b','ollama/gemma4:26b')][string]$OllamaModel='ollama/gpt-oss:20b'
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
Set-Location $root
python -m tools.coordination.lane_coordination validate
if($LASTEXITCODE -ne 0){ throw 'Lane coordination validation failed.' }
Start-Process powershell.exe -ArgumentList @('-NoExit','-ExecutionPolicy','Bypass','-File',(Join-Path $root 'Start-CodexLane.ps1'))
Start-Process powershell.exe -ArgumentList @('-NoExit','-ExecutionPolicy','Bypass','-File',(Join-Path $root 'Start-AntigravityLane.ps1'))
if($IncludeOllama){
  Start-Process powershell.exe -ArgumentList @('-NoExit','-ExecutionPolicy','Bypass','-File',(Join-Path $root 'Start-OllamaLane.ps1'),'-Model',$OllamaModel)
}
Write-Host 'Parallel agent processes launched. Claims and the integration lock remain authoritative.'
