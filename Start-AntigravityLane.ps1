param([switch]$NoNewWindow)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$env:MJOBS_LANE='antigravity'
$prompt='Read prompts/PARALLEL_ANTIGRAVITY_LANE.md and execute it. Reconcile live main and all lane claims first; create a dedicated Antigravity feature worktree before any source mutation.'
$args=@('--model','claude-opus-4-6-thinking','--effort','max','--mode','accept-edits','--dangerously-skip-permissions','--prompt-interactive',$prompt)
if($NoNewWindow){ Set-Location $root; & agy @args; exit $LASTEXITCODE }
Start-Process powershell.exe -ArgumentList @('-NoExit','-Command',('Set-Location "'+$root+'"; $env:MJOBS_LANE="antigravity"; agy '+(($args | ForEach-Object {'"'+($_ -replace '"','\"')+'"'}) -join ' ')))
