# Codex parallel lane

Set/assume `MJOBS_LANE=codex`.

Read `AGENTS.md`, `planning/lanes/README.md`, your lane CONTINUATION/STATE/QUEUE/
CLAIMS/HANDOFF, the Antigravity CLAIMS/STATE read-only, then live Git/GitHub truth.

Resume the active pushed Codex checkpoint before claiming another issue. During
feature work write only your feature paths, unique evidence, and
`planning/lanes/codex/*`. Do not edit canonical planning files.

Before integration, acquire the local integration lock using
`python -m tools.coordination.lane_coordination acquire-lock --lane codex`.
Only the lock holder may fast-forward root main or reconcile canonical planning.
Release the lock immediately after remote/local main and canonical state agree.

Never overlap Antigravity active claims. Never delete another worktree, force-push
another lane, or treat a stale worktree as active without claim/session evidence.
GitHub Actions/reviewer agents remain disabled. External submission remains OFF.


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
