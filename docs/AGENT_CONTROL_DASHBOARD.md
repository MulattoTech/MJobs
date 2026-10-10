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


## Live terminal streaming

Each agent card includes a persistent live terminal backed by a WebSocket. It streams
bytes from the current supervised run rather than polling a small tail. Reconnecting
starts at the current run's recorded byte offset rather than replaying unrelated
historical runs.

Antigravity is launched with a per-run native `--log-file`. Its terminal merges the
normal print-mode output with the native internal log, exposing API requests, quota
refresh behavior, 429/RESOURCE_EXHAUSTED responses, statusline/runtime errors, and
other internal execution details. The dashboard scans only the current run's logs for
quota errors, so stale historical quota failures cannot falsely mark a new run.

Codex streams its supervised execution output. Ollama/OpenCode is launched with
`--print-logs --log-level INFO --thinking` so the local lane exposes a denser live
execution stream.

The terminal supports pause/resume without dropping received bytes, follow/autoscroll,
clear-view, expand/collapse, source labels, and a heartbeat while an agent is quiet.


## Expanded Ollama / OpenCode statistics

The Ollama card reads OpenCode's structured local SQLite records instead of estimating
token counts from terminal text. It separates the newest session from cumulative
usage for the claimed worktree and the selected local model.

Current-session metrics include context tokens, new input/output/reasoning tokens,
cache read/write, cumulative processed tokens, assistant turns, tool-call count and
tool breakdown, context utilization, elapsed/model-turn wall time, cache share and
output-token throughput. Worktree totals aggregate all OpenCode sessions rooted at
that claimed worktree. Model totals include all-time and last-24-hour usage for the
selected Ollama model.

Ollama runtime information (loaded model, memory size, processor/offload description,
context window, keepalive) and observed Windows GPU utilization are shown separately
from token accounting.


## Codex deep telemetry

The Codex card reads the local Codex rollout JSONL records in addition to the account rate-limit RPC. It keeps four scopes separate:

1. **Current model turn** — current context/turn total, context-window utilization, new versus cached input, cache-write input, output and reasoning output.
2. **Current Codex session** — cumulative processed/input/cache/output/reasoning tokens, model turns, structured tool calls, cache share and wall-clock throughput.
3. **Claimed MJobs worktree** — aggregate session/token/tool totals for Codex rollout files whose recorded cwd matches the claimed worktree.
4. **Selected Codex model on MJobs** — all-time and last-24-hour aggregates for that model across Codex sessions rooted in the MJobs repository.

Codex input_tokens includes cached input. The dashboard therefore also reports new_input_tokens = input_tokens - cached_input_tokens; it does not double-count cached context as new prompt material. The account quota bar remains sourced from account/rateLimits/read, not from these token totals.

## Antigravity deep telemetry

Antigravity transcript JSONL records exact input_tokens, cache_read_tokens, and output_tokens on each PLANNER_RESPONSE. The dashboard scopes transcripts to the claimed MJobs worktree and reports:

- latest prompt/context material (input + cache-read) and latest output;
- conversation cumulative input/cache/output/processed tokens;
- planner turns, generic/tool-result steps and failed steps;
- tool-call count and per-tool breakdown;
- thinking-step count plus thinking text characters/words;
- cache share, elapsed conversation time and wall-clock output-token throughput;
- aggregate totals across all conversations tied to the same MJobs worktree;
- configured-model historical/24-hour totals only when the model named in the recorded lane launch prompt matches the configured dashboard model.

Antigravity does not expose hidden thinking-token counts or a reliable remaining quota percentage through its local CLI. The dashboard therefore never invents those numbers. It shows thinking-text statistics and native quota/429 evidence instead. Historical model grouping is explicitly marked as launch-prompt attribution rather than provider-billed model accounting.


## Lane idle-state semantics

A completed/released lane with `planning/lanes/<lane>/CONTINUATION.json` at
`IDLE_READY` and no live machine claim is displayed as `IDLE_UNASSIGNED`.
A prior supervisor stop/restart does not masquerade as a user block. Explicit stop
actions record `STOP_REQUESTED`. A writable lane still requires a live claim before
Start; this preserves worktree/path isolation instead of silently choosing arbitrary
GitHub work.


## Mission Control v2 — GUI launch studio and acceptance tracker

**Preferred startup:** use the **MJobs Mission Control** desktop shortcut, opening
`http://127.0.0.1:18726/`. This uses the same loopback-only supervisor plus a
new Launch Studio with selectable Codex, Antigravity and Claude model/profile
pairs, full-prompt preview, optional agent/instructions, an explicit Launch CLI
button and per-lane Stop. Legacy OpenCode/Ollama controls remain advanced and
are unavailable when the CLI has been uninstalled.

No root-main clean state is required. Existing isolated worktrees and uncommitted
changes are preserved, cached origin/main is used only if fetching fails,
and only clean behind-only lane branches may fast-forward. No automatic
rebase, merge of diverged history, main mutation, or issue-claim takeover.

The live Release Acceptance panel uses the **exact same canonical acceptance
matrices** as the `mjobs-progress` ChatGPT skill, not a guess from commits.
It refreshes read-only around every 11 seconds, shows separate MJ-REQ/MJ-REF
percentages, under-accepted feature areas, source timestamps, checkpoint
freshness and remote GitHub SHA. Accepted changes still require an integration
owner to update and publish the generated card. See
[MISSION_CONTROL_LAUNCHER.md](MISSION_CONTROL_LAUNCHER.md).
