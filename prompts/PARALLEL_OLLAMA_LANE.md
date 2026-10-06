# Ollama local-GPU parallel lane

Set/assume `MJOBS_LANE=ollama`.

Runner: OpenCode with local Ollama.
Preferred model: `ollama/gpt-oss:20b`.
Fallback for harder bounded work: `ollama/gemma4:26b`.

Read `AGENTS.md`, `planning/lanes/README.md`, your own lane continuation/state/
queue/claims/handoff, and BOTH Codex and Antigravity claims/state read-only. Then
reconcile live Git/GitHub/worktrees.

Your initial candidate is issue #318 / MJ-REQ-063.D01 Greenhouse ATS detection.
Recheck live ownership and dependency readiness before claiming it.

Choose only work that overlaps neither cloud lane. Prefer small, deterministic,
test-heavy vertical slices: isolated adapters, validators, persistence helpers, UI
subcomponents, regression fixes, or evidence-backed reference gaps.

Create `ollama/<slug>-<date>` and a dedicated worktree before source mutation.
During implementation write only claimed feature paths, unique evidence, and
`planning/lanes/ollama/*`. Do not edit canonical planning files.

Before integration acquire:
`python -m tools.coordination.lane_coordination acquire-lock --lane ollama`.
Only the lock holder may update canonical planning or root main. Release immediately
after local/remote main agree.

Never overlap active claims, never force-push main, never use `git add -A`, and do
not perform external job submission. GitHub Actions/reviewer agents remain disabled.
If the task becomes architecturally ambiguous or exceeds local-model confidence,
checkpoint safely, mark it for Codex/Antigravity, release the claim, and select a
smaller independent issue.
