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
