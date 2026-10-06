# Parallel agent lanes

Canonical files under `planning/` remain **integration truth**. They are not normal
feature-branch scratchpads when Codex and Antigravity are running concurrently.

Each active implementation agent owns exactly one lane:

- `planning/lanes/codex/`
- `planning/lanes/antigravity/`
- `planning/lanes/ollama/`

Each lane contains:

- `CONTINUATION.json` — restart identity/model/current checkpoint;
- `IMPLEMENTATION_STATE.json` — compact lane-local operational state;
- `PR_QUEUE.json` — only work claimed/queued by that lane;
- `CLAIMS.json` — active path/issue ownership declarations;
- `SESSION_HANDOFF.md` — human-readable resume instructions.

## Write ownership

An agent may write only its own lane files while implementing. It may read the other
lane to avoid overlap.

These canonical files are **integration-lock owned**:

- `planning/IMPLEMENTATION_STATE.json`
- `planning/PR_QUEUE.json`
- `planning/CAPABILITY_MATRIX.json`
- `planning/SESSION_HANDOFF.md`

Feature branches should put acceptance evidence in unique `docs/evidence/*` files and
update their lane-local state. Canonical planning is reconciled only while holding the
local integration lock.

## Integration lock

Use:

```powershell
python -m tools.coordination.lane_coordination lock-status
python -m tools.coordination.lane_coordination acquire-lock --lane codex
python -m tools.coordination.lane_coordination release-lock --lane codex
```

The lock is stored under the shared Git common directory and is intentionally not
versioned. It coordinates concurrent agents on this machine. Never delete another
live lane's lock merely to make progress; inspect owner/PID/time and stop if ownership
is unclear.

## Claims

Before source mutation, record a lane claim and post the same sanitized claim on the
GitHub issue. Claims must include issue, requirement, branch, worktree and expected
paths. Do not choose work overlapping another lane's active paths.

Run:

```powershell
python -m tools.coordination.lane_coordination validate
python -m tools.coordination.lane_coordination status
```

before selecting each new issue and before integration.
