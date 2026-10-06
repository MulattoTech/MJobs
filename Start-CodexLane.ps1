param([switch]$NoNewWindow)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$env:MJOBS_LANE='codex'
$prompt='Read prompts/PARALLEL_CODEX_LANE.md and execute it. Reconcile live main and all lane claims first; create a dedicated Codex feature worktree before any source mutation.'
$args=@('-C',$root,'-m','gpt-6-luna','-c','model_reasoning_effort="high"','-s','workspace-write','--approve-for-me',$prompt)
if($NoNewWindow){ & codex @args; exit $LASTEXITCODE }
Start-Process powershell.exe -ArgumentList @('-NoExit','-Command',('Set-Location "'+$root+'"; $env:MJOBS_LANE="codex"; codex '+(($args | ForEach-Object {'"'+($_ -replace '"','\"')+'"'}) -join ' ')))
