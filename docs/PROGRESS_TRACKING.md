# MJobs progress tracking: batched, low-token, agent-neutral

This contract applies to **every agent/tool writing MJobs**, including Codex/Luna, Claude Code, Gemini/Antigravity, Ollama and human sessions. `AGENTS.md` is the project authority; this document describes the tracker-specific implementation. A worker must receive the contract through the root brief if its tool does not load AGENTS.md automatically.

## Truth hierarchy

1. Current user scope, current Git/GitHub state, exact accepted source and test evidence.
2. Original acceptance: `planning/CAPABILITY_MATRIX.json` (**all 366 MJ-REQ IDs**, including children).
3. Additive acceptance: `planning/REFERENCE_GAP_MATRIX.json` (**59 MJ-REF IDs**); never fold into original denominator.
4. Queue/handoff: `planning/PR_QUEUE.json`, `planning/IMPLEMENTATION_STATE.json`, `planning/SESSION_HANDOFF.md`.
5. Readable view: `docs/PROGRESS_DASHBOARD.md`, generated deterministically from the matrices and the `planning/ISSUE_SEED.jsonl` area/priority mapping.

`docs/PROGRESS_CARD.json` and `docs/PROGRESS_CARD.svg` are small, derived companions to the Markdown dashboard. The existing `progress_dashboard --write` refreshes all three at the same authorized checkpoint, and `--check` verifies them together. Do not run a separate timer, hook, hosted CI job, or auto-push.

The dashboard is **derived, not an alternative source of truth**. PASS means the full acceptance for that particular ID has been proven. A merged limited slice remains PARTIAL if its parent requirement is incomplete; NOT_RUN does not prove absence of source code. Local validation never masquerades as hosted verification or full-release acceptance.

## Refresh cadence: once per meaningful integration batch, not every turn

- **Worker/child at bounded task completion:** return a concise issue/requirement ID, acceptance result, exact commit/source tree, test evidence path, unverified gaps, and owned branch. Do **not** edit canonical matrices or the dashboard. Only write task-owned code/tests/unique sanitized evidence or authorized lane-local records.
- **Root/integrator after accepting a batch of verified source slices:** while holding the existing integration lock, reconcile GitHub issue state, update the *affected* matrix entries and queue/state fields (do not rewrite unaffected statuses), and render the dashboard **once** using the deterministic command below. Multiple child results can be folded into a single checkpoint.
- **Root at session handoff / pause:** if accepted source/state has changed since the last rendered dashboard, refresh it once, validate, and include links/checkpoint. If nothing material changed, only run `--check`; do not create no-op commits.
- **Review/read-only agent:** use `--check` if relevant, report stale state but do not mutate without an integration-owner claim.
- **Never** regenerate on every model turn, token budget increment, test rerun, unmerged commit, worker chat message, or wall-clock timer. No background watchers, Git hooks, scheduled runners, paid CI or per-subagent summaries into a duplicated global tracker.

This cadence is a default; an explicit user request to refresh now can trigger one owned refresh. Aim for one brief tracker update per integrated *batch* (or per complete session if fewer updates occur), not one update per commit.

## Deterministic commands (repo root)

```powershell
# Lightweight, read-only single-line summary for agents (no large JSON in context).
python -m tools.verification.progress_dashboard --summary

# Read-only: fail if the dashboard lags its canonical inputs.
python -m tools.verification.progress_dashboard --check

# Integration owner only, with the normal local integration lock:
python -m tools.verification.progress_dashboard --write

# Required normal validations (run only when inputs/acceptance are changed):
python -m tools.verification.catalogue
python -m tools.verification.progress validate
python -m unittest tests.unit.test_progress_dashboard
git diff --check
```

The renderer checks the exact original and reference ID sets, rejects unknown statuses, derives area/priority totals from the issue seed, carries forward source timestamps, and leaves byte-identical output untouched. It does **not** infer requirement PASS from commit messages, tests or GitHub closures.

## Race safety and publication

Only one integration owner updates the four canonical planning files and `docs/PROGRESS_DASHBOARD.md` at a time. Always inspect dirty/staged/untracked changes, claim ownership of the existing integration lock, and validate the combined tree before a publish. Never overwrite an active writer's matrix; if the lock or ownership is uncertain, record "TRACKER_REFRESH_PENDING" with cause and exact known commit in the lane handoff, then leave existing files intact. Do not reset, clean, stash, force-push or edit main to bypass ownership.

A generated dashboard uses **committed source matrices as of that checkpoint**, not an optimistic prediction about unfinished child work. Update the root plan and evidence first, generate once, commit/publish by local-only policy, then check that remote/main and the displayed tracker agree. If main moved during the refresh, reconcile and rerender after merging; never claim a branch-only dashboard is already live.

## Minimum checkpoint note (no repetitive essays)

`issue(s) | fully accepted/partial | changed MJ-REQ/MJ-REF IDs | source SHA/tree | local verification and NOT_RUN gates | merged/main SHA | dashboard refreshed or unchanged/pending | next core workflow`

During ordinary work, prefer this compact receipt and a meaningful tested user workflow to long per-turn progress narratives. Preserve private test output and resumes outside public evidence.
