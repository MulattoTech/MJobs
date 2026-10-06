param(
  [ValidateSet('ollama/gpt-oss:20b','ollama/gemma4:26b')][string]$Model='ollama/gpt-oss:20b',
  [switch]$NoNewWindow
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$env:MJOBS_LANE='ollama'
if(-not (Get-Command opencode -ErrorAction SilentlyContinue)){ throw 'OpenCode is not installed.' }
if(-not (Get-Command ollama -ErrorAction SilentlyContinue)){ throw 'Ollama is not installed.' }
$models=@(opencode models ollama)
if($models -notcontains $Model){ throw "OpenCode cannot see local model $Model" }
$prompt='Read prompts/PARALLEL_OLLAMA_LANE.md and execute it. Reconcile live main and both cloud-agent claims first; create a dedicated Ollama feature worktree before any source mutation.'
$args=@($root,'--pure','--model',$Model,'--auto','--prompt',$prompt)
if($NoNewWindow){ & opencode @args; exit $LASTEXITCODE }
Start-Process powershell.exe -ArgumentList @('-NoExit','-Command',('Set-Location "'+$root+'"; $env:MJOBS_LANE="ollama"; opencode '+(($args | ForEach-Object {'"'+($_ -replace '"','\"')+'"'}) -join ' ')))
