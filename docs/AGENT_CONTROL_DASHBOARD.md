# MJobs Agent Control Dashboard

The MJobs agent-control dashboard is a local, loopback-only control surface for the
parallel Codex, Antigravity, and Ollama/OpenCode implementation lanes. It uses the
existing repository lane claims and atomic integration lock. It is a normal FastAPI
server with plain HTML/CSS/JavaScript; there is no Electron runtime or frontend build.

## Start

From a non-elevated PowerShell in the repository root:

```powershell
.\Start-AgentDashboard.ps1
```

The dashboard binds only to `127.0.0.1:18704` and opens the browser. Use
`-NoOpen` when a browser should not be launched.

## Controls

Each lane card shows:

- current supervisor status and PID;
- claimed GitHub issue from the local coordination bus;
- restart count and latest exit reason;
- model and reasoning/effort setting;
- persistent prompt override;
- auto-continue setting;
- the latest captured log output;
- Start, Restart, Stop, and Save Settings actions.

Repository state, current claims, worktrees, and integration-lock ownership are shown
below the lane cards.

Dashboard settings are stored under the shared Git common directory at
`.git/mjobs-coordination/dashboard.json`. Runtime PIDs/status use
`.git/mjobs-coordination/dashboard-runtime.json`. They are deliberately not
versioned and are shared by all worktrees. Logs are written under
`outputs/agent-dashboard/`.

## Why Codex previously stopped

`codex exec` is a one-shot agent turn. The captured run did not crash: after
reconciling issue #111 it loaded the generic brainstorming skill, classified the
remaining production consumer as architectural, and obeyed that skill's hard human
approval gate. It therefore asked for design approval and returned to PowerShell.

Parallel lane prompts now explicitly distinguish already-catalogued MJobs issue work
from fresh product ideation. Existing issue/spec acceptance should be implemented
directly. A truly new unresolved architecture decision is checkpointed as
`BLOCKED_DESIGN`; the lane should select another safe unclaimed issue instead of
ending the entire burn-down.

The supervisor also detects approval-language exits. It reports
`WAITING_APPROVAL` instead of repeatedly restarting the same question.

## Auto-continue behavior

When `Auto-continue` is enabled, an agent process that exits cleanly is relaunched
after a short delay. Every new turn must reconcile live claims, branches, and GitHub
state before mutation. Nonzero exits, quota/usage exhaustion, environment blockers,
or approval gates stop automatic continuation and remain visible in the dashboard.

Three clean exits shorter than the configured fast-exit threshold within five minutes
trip `CRASH_LOOP_GUARD` and require manual review.

## Safety and integration

The dashboard does not bypass the MJobs coordination rules:

- source work occurs in per-lane worktrees;
- active path claims remain exclusive;
- canonical planning and `main` require the atomic integration lock;
- GitHub Actions/reviewer agents stay disabled under current user policy;
- external application submission remains OFF;
- Stop sends a graceful process-tree termination request before escalating.

The web server rejects non-loopback bind requests and allows only localhost host
headers.


## Verified activity and quota telemetry

The dashboard distinguishes a process being alive from a model actively producing output. Each lane reports:

- supervisor PID plus the descendant process tree;
- exact worktree and effective model;
- ACTIVE, QUIET, or STOPPED activity based on log freshness;
- session ID when the runner emits one (Codex does);
- aggregate resident memory for the supervised process tree;
- observed Windows GPU-engine activity for the supervised process tree;
- last-output age.

Codex quota comes from the local Codex app-server account/rateLimits/read RPC—the same rate-limit model consumed by Codex clients. It exposes used/remaining percentage, reset time/window, credits, plan type, and reset credits when the backend supplies them. This is account quota, not an estimate based on local token logs.

Antigravity CLI currently does not expose an equivalent local percentage endpoint. The dashboard therefore reports its process/model/activity proof and explicitly marks quota percentage unavailable rather than estimating it.

### Ollama GPU offload control

The Ollama lane exposes a 0–100% GPU offload cap. This is not a hard GPU utilization percentage. On restart, the supervisor reads the model's transformer block count and creates/reuses a local derived Ollama model with PARAMETER num_gpu <layers>. Example: Gemma4 26B reports 30 blocks, so a 50% offload cap uses 15 GPU layers. The derived alias is added to OpenCode's local Ollama model configuration and then used for the supervised lane.

Windows GPU engine counters are shown separately as observed utilization so the user can compare configured offload with actual activity.
