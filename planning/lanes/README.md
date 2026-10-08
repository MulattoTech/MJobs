# Parallel agent lanes

Canonical files under `planning/` remain **integration truth**. They are not normal
feature-branch scratchpads when Codex and Antigravity are running concurrently.

Each active implementation CLI owns exactly one lane:

- `planning/lanes/codex/`
- `planning/lanes/antigravity/`
- `planning/lanes/ollama/`

For concurrent Codex child agents, the root integrator owns the shared Codex lane
files. Each child uses a unique child-ID claim and issue comment, writes only its
exclusive source/test/evidence paths, and must not modify `planning/lanes/codex/**`
or canonical planning. This prevents children in the same Codex lane from racing on
one lane handoff while retaining independent issue/path claims.

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

The local claim bus supports multiple concurrent Codex children by a stable child ID.
Keep the legacy lane-level claim for existing sessions; use `--child-id` for each new
Codex child, and release only that child with the same ID. Claim updates are serialized
across local processes, active path overlap is rejected across all lanes, and an
existing claim ID cannot be replaced in place.

Run:

```powershell
python -m tools.coordination.lane_coordination validate
python -m tools.coordination.lane_coordination status
python -m tools.coordination.lane_coordination claim --lane codex --child-id req095-tech-rewrite --issue 96 --requirement MJ-REQ-095 --branch codex/mj-req095-tech-rewrite --worktree .worktrees/codex-mj-req095-tech-rewrite --path services/documents/tailor.py
python -m tools.coordination.lane_coordination release-claim --lane codex --child-id req095-tech-rewrite
```

before selecting each new issue and before integration.
