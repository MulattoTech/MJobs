# Antigravity parallel lane

Set/assume `MJOBS_LANE=antigravity`. Preferred model: Claude Opus 4.6 Thinking.

Read `AGENTS.md`, `planning/lanes/README.md`, your lane CONTINUATION/STATE/QUEUE/
CLAIMS/HANDOFF, Codex CLAIMS/STATE read-only, then live Git/GitHub truth.

Choose only a dependency-ready issue that does not overlap Codex claims or dirty
Codex worktrees. Create a dedicated `antigravity/*` feature branch/worktree before
source mutation. During implementation write only feature paths, unique evidence,
and `planning/lanes/antigravity/*`. Do not edit canonical planning files.

Before integration, acquire the local integration lock using
`python -m tools.coordination.lane_coordination acquire-lock --lane antigravity`.
Only the lock holder may fast-forward root main or reconcile canonical planning.
Release immediately after remote/local main and canonical state agree.

Prefer independent public source/ATS lanes while Codex owns AI/persistence. Never
use `git add -A` or `git push -f`; stage explicit paths and, if absolutely needed
on your own branch, use force-with-lease only after verifying remote ownership.
GitHub Actions/reviewer agents remain disabled. External submission remains OFF.
