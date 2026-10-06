$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$env:MJOBS_LANE='codex'
$prompt='Read prompts/PARALLEL_CODEX_LANE.md and execute it now. Reconcile live main and all lane claims first. Honor any existing Codex machine-local claim; create its dedicated feature worktree before source mutation. Work the claimed issue to a coherent locally validated pushed checkpoint and obey the integration lock before canonical planning/main changes.'
Set-Location $root
& codex exec -C $root -m gpt-6-luna -c 'model_reasoning_effort="high"' --approve-for-me $prompt
exit $LASTEXITCODE
