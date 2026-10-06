$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$env:MJOBS_LANE='antigravity'
$prompt='Read prompts/PARALLEL_ANTIGRAVITY_LANE.md and execute it now. Reconcile live main and all lane claims first. Honor any existing Antigravity machine-local claim; create its dedicated feature worktree before source mutation. Work the claimed issue to a coherent locally validated pushed checkpoint and obey the integration lock before canonical planning/main changes.'
Set-Location $root
& agy --print $prompt --model claude-opus-4-6-thinking --mode accept-edits --dangerously-skip-permissions
exit $LASTEXITCODE
