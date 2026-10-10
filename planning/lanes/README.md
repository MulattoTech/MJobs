# Parallel agent lanes

Canonical files under `planning/` remain **integration truth**. They are not normal
feature-branch scratchpads when Codex and Antigravity are running concurrently.

Each active implementation CLI owns exactly one lane:

- `planning/lanes/codex/`
- `planning/lanes/antigravity/`
- `planning/lanes/ollama/` (legacy OpenCode, optional)
- `planning/lanes/claude/` (third installed CLI, delivery/test harness)

For concurrent Codex, Antigravity and Claude sessions, use a unique child/session
ID for the machine-local claim and mirror the claim on the owning GitHub issue.
Each session writes only exclusive claimed paths and unique sanitized evidence.
One integrator owns canonical planning, shared APIs and root main. Existing older
lane-level claims are preserved rather than replaced by a new session.

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

The local claim bus supports scoped concurrent sessions in any registered lane
(Codex, Antigravity, Claude or legacy Ollama) using a stable unique child ID.
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
