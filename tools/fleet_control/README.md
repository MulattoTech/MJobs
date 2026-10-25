# MJobs Fleet Control

Local Orchestrator.inc controls for MJobs, with a shared entry point in Mission Control.

## Open and start

- Mission Control: http://127.0.0.1:18729/
- Fleet controls on the same browser port: http://127.0.0.1:18729/fleet/
- Direct fallback: http://127.0.0.1:8787/
- Desktop shortcut: **MJobs Fleet Control**.
- Start the dashboards: `pwsh -NoProfile -File C:\Tools\AO-Fleet-Control\Start-Fleet-Control.ps1 -OpenBrowser`.
- Dashboard startup never launches AO agents or enables dispatch.
- Mission Control proxies the private Fleet service on port 8787. Both bind to loopback; the shared address does not expose either service to the LAN.

## Tonight's startup sequence

1. Reset the Codex allowance yourself, then wait for a **fresh** quota reading above 15%.
2. Inspect the saved agent plans and the preserved roster.
3. Release the emergency latch with the explicit **REARM** control. This alone starts nothing.
4. Arm the quota guard at **7% remaining**. New work also requires the separate 15% reserve.
5. Restore the saved fleet. Verify the actual runtimes and provider selections.
6. Enable either Turbo or Sustained, or send one bounded manual instruction. Turbo and Sustained are mutually exclusive.

The saved emergency latch remains authoritative across dashboard restarts. A restored allowance alone does not start work. Stale, unavailable, malformed, expired or future-dated telemetry blocks new dispatch. Resetting a displayed timer is not proof of a provider allowance reset.

## Usage and the 7% stop

The isolated Codex App Server supplies read-only account/rateLimits/read telemetry; no inference turn is used to read it. The monitor polls every five seconds by default. Mission Control reuses this feed instead of creating an additional visible Codex window.

- **15% reserve:** stop issuing new assignments before the emergency threshold.
- **7% emergency:** the independent guard trips the persistent emergency latch and stops Codex/AO process trees.
- Unknown usage is never displayed as 100% available.
- The UI reports observation age, stale state and missing provider windows.
- Provider rounding, delayed reporting and in-flight work mean no client can promise an exact 7% billing balance.
- Emergency shutdown affects Codex and AO across this Windows account/machine. Unsaved in-memory work may be interrupted. Worktrees, branches and PRs are preserved.

Emergency Stop requires the displayed exact confirmation. A separate PowerShell watchdog enforces the latch even when the web server is stopped. It has a singleton mutex to prevent duplicate watchers. Residual protected processes are reported; the UI may claim zero only when a fresh watchdog enumeration confirms it. Shared Codex app-server and exec-server processes are included in the stop. The only exception is the isolated quota monitor, verified by exact executable, arguments, parent, creation times and fresh guard heartbeat; its RPC transport accepts only initialization and rate-limit reads.

The PowerShell Codex wrapper loads `Codex-Profile-Guard.ps1`: a latched stop blocks new working Codex launches, and the shared background server uses explicit no-console process flags and a startup mutex. Existing PowerShell windows retain previously loaded profile functions; open a new terminal after a profile update. Help and account-management commands remain available.

## Agent configuration and mixed providers

**Manage > Provider/model/reasoning** stages a plan even while the daemon is stopped. Saving a plan starts no worker. Draft selections and unsent instructions survive dashboard refreshes.

**Apply saved plan** is a separate confirmed operation requiring fresh state and a ready preflight:
- Only an idle or exited worker can be replaced.
- The original runtime is parked, and its branch, worktree, PRs and uncommitted work remain intact.
- A new isolated session is created with the selected harness, model and terminal/chat mode.
- The new identity, harness and model must verify before the original is marked replaced.
- An uncertain spawn is marked unverified and is not blindly retried.
- Primary-controller changes require a controlled handoff; the worker replacement action deliberately does not replace the primary.
- Existing PR ownership is not discarded or silently transferred to the new worker. Coordinate integration before assigning overlapping files.

Supported local choices are discovered from the installed clients. AGY Gemini Low, Medium and High use the CLI's native model variants; an unsupported separate effort flag is not sent. Standalone Gemini CLI is a separate client and is not assumed installed.

### Google/AGY usage

The installed AGY statusline hook copies only native quota fields into a local sanitized snapshot. It retains no prompts, credentials, email addresses or transcript paths. It preserves the normal AGY statusline.

- Use AGY terminal/TUI mode for the native statusline observation. Headless chat may not emit it.
- Named buckets show the provider's actual remaining fraction or amount and reset time.
- Until the first native observation, the dashboard says **unavailable**, not 0% or 100%.
- Observations older than 90 seconds are labelled **stale**; an old value is not treated as current.
- AGY's native `/usage` refreshes its quota display. The provider does not supply an independent sample timestamp in this hook.
- AGY quota is not standalone Gemini CLI quota. A separate Gemini client needs its own verified adapter.
- The Codex 7% auto-stop is not advertised as a verified Google percentage cutoff.

## Nudge, Turbo, Sustained and Find Work

- Manual nudge/custom instruction sends one bounded instruction after preflight and ownership checks.
- **All Hands** is one confirmed burst; it does not turn on a continuous mode.
- Turbo schedules bounded automatic dispatch within its configured budget.
- Sustained refills idle capacity under its configured worker/resource limits.
- Turning either continuous mode on turns the other off. Stop requests remain available while a cycle is busy.
- Find Work prioritizes an agent's existing CI failures, PR completion and review before new implementation.
- Refreshing or opening the dashboard never nudges an agent.
- All send paths share a persistent two-minute per-session reservation. An uncertain delivery is not automatically resent after a timeout or server restart.
- Active-turn observations, fresh GitHub state, free memory/disk and the quota gate are checked before dispatch. An exited historical primary is not a live coordinator.
- Manual commands serialize with automatic dispatch. A queued operation reports its result; message acceptance alone is not completion.

**Pause Dispatch** stops dashboard assignment loops but does not interrupt active agents. **Graceful Wind-down** stops intake, requests a checkpoint and exits idle runtimes. **Emergency Stop** enforces the independent kill latch. Recovery preserves the frozen manifest; replaced original sessions are excluded.

## Accuracy and refresh

The roster refreshes independently of pending writes. Concurrent refresh requests coalesce, GETs have deadlines and failed reads retain a clearly labelled stale snapshot. Writes have bounded waits and are never automatically retried. Offline PR/CI data is not labelled live. Configuration can be staged offline; execution needs fresh preflight.

## Source, tests and deployment

The reviewed source is tracked in the MJobs repository under `tools/fleet_control`. The installed runtime directory is `C:\Tools\AO-Fleet-Control`.

Do not copy runtime JSON, logs, backups, browser profiles, provider settings or credentials into Git. Update installed source only while the emergency latch is active or all dispatch is paused, preserving the runtime state files. Restart the dashboard/monitor intentionally; never replace a live worker's source tree.

Run `node check-fleet.js` from the installed source directory. The allowlisted suites use fixtures and no live inference. The report records the source fingerprint and fails if source changes during the checks. Tests containing live browser actions are excluded. Read-only browser verification must not POST actions.

Important local evidence:
- `diagnostics/preflight-results.json`: test results and source fingerprint
- `audit.jsonl`: control actions
- `dispatch-ledger.json`: duplicate-send reservations (see DispatchLedger filename in source)
- `agent-plans.json` and `replacement-state.json`: saved plans and verified replacement mappings
- `quota-guard-status.json`, `emergency-status.json`: independent guard/watchdog observations
- `restore-snapshot.json`: preserved recovery manifest

Read-only APIs: `/api/health`, `/api/state`, `/api/preflight`, `/api/providers`, `/api/gemini-usage`, `/api/quota`, `/api/agent-plan`. Mission Control exposes them under `/fleet/api/...`.

This dashboard does not bypass provider quotas, GitHub branch protections, acceptance gates, ownership rules or application-delivery approvals.
