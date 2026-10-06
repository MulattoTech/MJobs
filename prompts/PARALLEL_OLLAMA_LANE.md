# Ollama local-GPU parallel lane

Set/assume `MJOBS_LANE=ollama`.

Runner: OpenCode with local Ollama.
Preferred model: `ollama/gemma4:26b`.
Fallback for lighter/faster bounded work: `ollama/gpt-oss:20b`.

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


## Unattended existing-issue execution

This lane is a continuous implementation worker for requirements already catalogued in
GitHub and the repository specs. That work is not fresh product brainstorming. Do not
invoke the generic brainstorming/writing-plans approval workflow merely because an
existing issue spans multiple modules, and do not stop to ask for routine design/spec/
plan approval when the issue/spec already defines the acceptance boundary.

Use the existing architecture and implement the smallest coherent acceptance-backed
vertical slice. If you discover a genuinely new architectural decision that is not
settled by the issue, area spec, existing code, or current user authorization, do not
invent it and do not terminate the whole lane: checkpoint safely, mark the slice
`BLOCKED_DESIGN` in the lane handoff/log, release its claim if appropriate, and
select another non-overlapping dependency-ready issue.

After a successful issue/slice, continue directly to the next dependency-ready
unclaimed issue until stopped, quota-blocked, or no safe work remains.
