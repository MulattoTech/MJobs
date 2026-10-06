param(
  [ValidateSet('ollama/gpt-oss:20b','ollama/gemma4:26b')][string]$Model='ollama/gpt-oss:20b'
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$env:MJOBS_LANE='ollama'
if(-not (Get-Command opencode -ErrorAction SilentlyContinue)){ throw 'OpenCode is not installed.' }
if(-not (Get-Command ollama -ErrorAction SilentlyContinue)){ throw 'Ollama is not installed.' }
$models=@(opencode models ollama)
if($models -notcontains $Model){ throw "OpenCode cannot see local model $Model" }
$prompt='Read prompts/PARALLEL_OLLAMA_LANE.md and execute it now. Reconcile live main and both cloud-agent claims first. Honor any existing Ollama machine-local claim; create its dedicated feature worktree before source mutation. Work the claimed issue to a coherent locally validated pushed checkpoint and obey the integration lock before canonical planning/main changes.'
Set-Location $root
& opencode run --pure --model $Model --auto $prompt
exit $LASTEXITCODE
