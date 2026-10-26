# Fleet operations, runtime truth and agent details — 26 October 2026

## Outcome and boundaries

This change extends the preserved-fleet recovery in PR #825. It is installed in
Fleet Control and the protected remote status hub, and updates the local Mission
Control launch board without writing dashboard source into the active main
checkout. It does not replace or restart native AO/Codex agent processes.

The observed feed at `2026-10-26T03:57:30.056Z` had 31/31 verified runtimes:
30 terminal-backed agents and one Chat controller. Sustained dispatch was enabled,
three agents were working, and the allowance was 75%. These are timestamped
observations, not a promise that activity or allowance remains constant.

**Direct remote command controls are not enabled.** An attempted authenticated
remote-control backend write was blocked by a tool safety check; it was not
retried through another route and no command proxy was installed. The remote page
states this limitation. Local pause/resume, concurrency settings, status requests
and existing agent management remain available through the loopback controls.

**The native AO startup warnings remain unresolved.** The corrected PowerShell
shell entry point explicitly chooses embedded mode for per-invocation overrides,
but AO starts the native executable directly and supplies its own arguments.
No native AO binary was patched and no hook-trust warning was hidden.

## Runtime and controller presentation

TUI sessions do not have a Chat controller. A null Chat-controller field previously
became a misleading generic `Unknown` in the remote table. The projection now
separates interface type, native runtime verification, and activity:

- `Terminal · verified` means a terminal-backed session was present in a fresh
  positive runtime inspection. It does not invent a Chat-controller value.
- `Chat · ready` or the reported Chat state retains its actual controller evidence.
- Missing, stale, future, pending or unrelated inspections cannot confer native
  verification. Offline observations cannot retain positive live activity.

Provider cards aggregate actual AO-managed runtimes and retain the independent
Mission Control launcher's state separately. A stopped independent Codex slot no
longer makes an active 31-agent AO fleet appear stopped. Providers with no managed
sessions are labeled accordingly; the board does not claim unmanaged desktop
applications are globally stopped.

## Clickable agents

Every remote agent name opens a keyboard-accessible detail dialog. The local
Fleet name opens the existing Manage drawer. Local Mission Control includes
per-agent deep links into that drawer using `/fleet/#agent=<id>`.

The views include provider/model, interface and runtime state, branch, creation
and activity times, latest runtime verification, dispatcher-owned issue/file
reservations, tracked pull requests, and recent dispatcher actions. No recorded
lease means **no dispatcher-owned assignment is recorded**, not proof that the
primary never assigned a task directly.

The export remains an allowlist: no prompt text, raw terminal/history content,
command lines, credentials, local absolute paths, native conversation UUIDs or
raw errors are added. GitHub links are generated from validated numeric issue/PR
identities. Rendering uses text nodes, and source freshness expires independently
of the publication timestamp. Opening a detail view does not send an agent turn.

## Fewer repetitive assignments

Automatic dispatch now uses durable event identities instead of treating idle
elapsed time as permission to repeat the same work. The new shared ledger binds
PR actions to exact heads and relevant check/review outcomes. Reservations are
persisted before delivery and survive process restart and uncertain outcomes.
Unreadable or full ledgers fail closed rather than forgetting previous sends.

Pending or absent hosted checks do not cause repeated owner audits. A concrete
failed check can still be actionable while other checks run. The same failed
head is not repeatedly assigned merely because a cooldown expired. Sustained and
Turbo share exact-head review protection, including explicit All Hands reviews.

An existing issue lease is not reissued on every idle cycle, and an actively
reserved worker is not sent repeated refill requests. Coordination identity
excludes busy/idle timestamps. Reports request only changed actionable facts,
with a single `NO_NEW_GAP` outcome when the current implementation already covers
the proposed slice. The scheduler does not manufacture production defects or
acceptance credit for duplicated tests.

These changes govern the Fleet Control dispatcher. They do not strip AO's
`ao-session-delivery` transport markers or assert that every independently sent
primary/worker message has been deduplicated. The referenced oversized paste
placeholders had no attached full text in the conversation and were not analyzed
as if their missing contents were available.

## Local launch-board controls

The local Mission Control page now provides pause-new-assignments, resume
Sustained, and a working-target control from 3 to 29 using the existing guarded
loopback Fleet API. Each control requires an explicit confirmation. Pause does
not kill active turns; changing the working target neither creates nor terminates
agents. Existing quota, restoration, ownership and resource guards remain.

The independent Codex launch UI is distinguished from the AO fleet, and its new
launch button is disabled while AO agents are observed to avoid an accidental
second integration owner. This is an additional UI precaution, not a claim that
unrelated native launch interfaces have been changed.

## Codex launch warnings

The installed native CLI supports `--no-daemon`. The PowerShell launcher now
recognizes `-c` / `--config`, `--enable`, `--disable` and `--search` as
per-invocation overrides and explicitly selects embedded mode for applicable
interactive/resume/fork invocations, preserving all supplied settings. Ordinary
eligible launches still use the shared server. This only affects that shell
entry point; already-running processes retain their original arguments.

Inspection of AO's native launch arguments and its pinned command builder showed
both session-specific configuration overrides and a hardcoded
`--dangerously-bypass-hook-trust`. AO's hooks provide activity/identity tracking;
blindly stripping these arguments could change configuration or break tracking.
This change does not remove the native flag, approve arbitrary hooks, suppress
its warning, or claim that AO invocations now use a shared backend.

Reviewed source references:

- [AO command builder](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/pkg/agentruntime/command.go)
- [AO Codex adapter](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/adapters/agent/codex/codex.go)

## Deployment and verification

The source and installed Fleet runners both passed **39/39 groups**, with zero
failed/unexecuted groups and stable source bytes. The source result was recorded
at `2026-10-26T04:01:00.700Z` with fingerprint:

```text
e0f7162185684ec93c5315d438cff5def4ea07c92f270799a1c422b7313c3c71
```

The fingerprint covers Fleet JavaScript, PowerShell, VBS, Python entry point and
local/remote Fleet assets. Mission Control static files are separately included
in the installed dashboard package manifest and publication diff.

Executed commands:

```powershell
node tools/fleet_control/check-fleet.js
python -m pytest -q -p no:cacheprovider tests/unit/test_fleet_mission_runtime.py tests/unit/test_fleet_proxy.py tests/unit/test_mission_control.py --tb=short
git diff --check
```

The Python selection passed **35 tests and 31 subtests**. New evidence includes
seven operational-projection tests, six dispatcher-policy tests, real PowerShell
5.1/7 shell fixtures with the override cases, existing startup/mutation safety
regressions, and the 15 remote-exporter tests.

The first dashboard-only deployment rolled back after the new Python entry point
incorrectly treated `tools` as a regular package instead of a PEP 420 namespace.
The correction supports both package forms and gained a fresh-process namespace
import regression. A read-only import of the actual reviewed dashboard snapshot
then succeeded before the complete test suite was rerun.

The successful deployment checkpoint was `2026-10-26T03:56:48.8247912Z`:
installed 39-group validation passed, the dashboard package contained 31 verified
files, and Sustained dispatch was restored. Native agent processes and their
conversations/worktrees were not restarted or changed by this deployment.

Mission Control executes a verified snapshot of `tools/agent_dashboard` under the
Fleet runtime root. `Run-Mission-Control.py` verifies each manifest hash and path
before importing it, while preserving the original repository data, venv,
persisted launcher state and unrelated shared tools. The hidden startup task
selects this entry point when the installed snapshot exists. Source backups and
runtime manifests are machine-local, not committed.

At `2026-10-26T03:58:29.764Z`, isolated headless-browser checks passed for all
**31 remote agent detail links**, the correct interface labels, the local
31-agent deep links/Manage drawer, four provider panels and the local control
buttons. The remote dialog fit a 390-pixel phone viewport without horizontal
overflow. There were **zero JavaScript exceptions and zero mutating requests**.
Mission Control's existing authenticated, read-only prompt-preview POST occurred;
no launch, instruction, pause/resume or other mutating control was clicked during
this browser inspection.

The remote hub assets and advancing origin feed were checked. Cloudflare's
existing owner-only Access policy and tunnel-side Access validation were read
and left unchanged. **An authenticated external browser sign-in was not
exercised**; origin rendering is not substituted for that check.

Hosted checks must be evaluated on the newly published head. Prior-head CI,
these focused local results, and UI existence do not by themselves constitute
full application acceptance or permission to bypass main protection.

A final import-order-only Python correction passed Ruff and the full source and installed 39-group gates again. The reviewed entry file was synchronized without another service or agent restart. Final installed verification: `2026-10-26T04:03:50.171Z`.
