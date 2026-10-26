# Fleet control operational audit — 26 October 2026

## Outcome

This user-authorized audit checked running and stopped-state behavior of Fleet Control, Mission Control, and the remote status publisher. The repair was installed without restarting native agents, changing provider permissions, modifying active main, or submitting model instructions.

At the live verification checkpoint, the AO daemon and both dashboards were online. There were 31 preserved MJobs targets: 30 healthy idle runtimes and one primary reporting `exited`. The primary's last reported activity was 2026-10-26T06:52:21.3132454Z; this audit did not establish the cause of its exit and did not restart it. Sustained remained configured on, Turbo off, and actual dispatch was correctly held because the primary was stopped. Allowance was 97%, with the acknowledged 7% stop guard and five-second polling intact.

The stopped primary is now present in the recovery preview: **31 targets, 30 already online, one requiring recovery, no conflicting primary**. Protected Startup remains the explicit recovery path. Assignment controls are not a substitute for restarting a stopped primary.

## Defects repaired

| Defect | Correction |
| --- | --- |
| Periodic snapshot capture forgot a previously eligible primary after its runtime stopped. | Retain only the exact previously eligible nonarchived identity, including native creation time. Preserve its historical observation and separately record the latest observed stop. No historical archive or unrelated stopped project is enrolled. |
| Capture could complete after a stop, freeze, restore transition, or competing snapshot write. | Recheck the live guards and the state/snapshot digests after the asynchronous read. An intervening change wins; capture makes no write. |
| An invalid recovery manifest was presented as an offline AO daemon. | Separate manifest, native-inspection, and daemon-read failures; unknown daemon availability is not falsely reported as down. |
| The enabled “Continue Startup” button returned without opening its confirmation. | Permit the resumable state to open the protected confirmation, retaining phrase checks, request identity, cancellation and duplicate-request protection. |
| Find Work rejected terminal agents legitimately waiting for an instruction. | Accept `waiting_input` alongside idle; still reject tool/approval decision states, active turns, exited or superseded agents. |
| A stop during the roster read could leave a new Find Work reservation. | Recheck the guard after the read and before reserving delivery. Reject offline fallback data. |
| Empty Find Work results could display undefined counts. | Return explicit zero submitted/uncertain counts and the actual skipped count. |
| Preflight and dashboard badges could imply usable execution while recovery was incomplete. | Gate execution on current runtime readiness, keep configuration staging available, and distinguish a scheduler enabled-but-held from productive work. |
| A graceful drain could be obscured by an old completed-restore badge. | Give current draining state precedence. Failed controller refreshes display unknown/unavailable rather than stale running badges. |
| Archive confirmation was not gated in the browser. | Require the exact original session ID before enabling or submitting the archive action; existing server preservation checks remain. |
| The remote hold message did not identify the stopped primary. | Publish an allowlisted stopped-primary explanation; do not export arbitrary backend errors. |

A one-time repair recovered only the lost primary from the latest verified pre-exit backup. Its ID, project, role, harness, model, mode, branch and creation time matched current native metadata exactly. The repair changed no restore ledger, conversation, native database or worktree. Private before/after hashes and the repair receipt remain on the workstation. Subsequent periodic snapshots retained all 31 targets while recording the primary as currently stopped; live health still reported only 30 verified.

## Executed validation

| Check | Result |
| --- | --- |
| Source Fleet regression runner | **41/41 groups passed**, zero failed or unexecuted groups; source stable. |
| Installed Fleet regression runner | **41/41 groups passed**. |
| Isolated real-browser button contracts | **36 scenarios passed**, 33 inert fixture POSTs, zero production requests, zero native agent commands, zero JavaScript exceptions. |
| Python dashboard/runtime/proxy regression | **35 tests and 31 subtests passed**. |
| Live direct and shared-port read routes | **24 endpoint checks passed** across ports 8787 and 18729. |
| Invalid action requests | Missing local action header and foreign origin rejected with 409; foreign Host rejected with 403. All requests also used an invalid confirmation phrase. No Startup action was authorized. |
| Canonical installation comparison | **120 Fleet code/assets matched the tested source bytes**. Historical local diagnostic helpers are not confused with canonical repository files. |
| Live UI | All **31 remote agent detail dialogs** opened; local agent deep links, four provider panels and phone layout worked. Startup showed 31 targets; assignment controls were disabled while the primary was stopped. Zero JavaScript exceptions or mutating requests. |
| Repeated actual dashboard launch | The Limited scheduled launcher completed twice with exit status zero; dashboard/AO listener PIDs and the Codex process set were unchanged. |
| Persistence | The repaired primary remained in subsequent periodic snapshots; the original completed-restore ledger was preserved. |
| Remote infrastructure | Hub tunnel healthy; Access policy restricted to the owner account. Unauthenticated HTML, JSON and JavaScript requests returned 403. |

The isolated browser suite serves only synthetic fixtures on a random loopback port and intercepts requests outside that origin. It never imports the production controller, executes AO/Codex, or forwards a control request to a live service. It exercises Startup/Continue/Recheck/Cancel, opening AO, pause/drain/cancel-drain, both emergency buttons, emergency recheck/rearm, both restore buttons, pause-restoration, Turbo controls/settings/cycle, All Hands, Sustained controls, quota configuration/arm/disarm, single/bulk Find Work, status/custom instructions, saved worker plans/apply, archive, offline configuration, blocked decisions and cancellation. A simulated emergency response proves UI routing/confirmation, not actual process termination.

Source fingerprint:

```text
20b9a1ed75fdb0b906a1b8b32f7371db2695a1adbebe2f388e03b686cde211fd
```

Deployment completed at **2026-10-26T12:11:26.0768230Z**. Live route/installation verification completed at **12:12:26Z**, live UI verification at **12:15:59Z**, and launcher idempotence at **12:16:47Z**. These timestamps identify observations, not guarantees of future agent health.

## Scope boundaries and remaining limitations

- Native AO still supplies per-invocation Codex overrides and hook-trust bypass flags. The live inspection counted 29 readable hook-bypass invocations and 32 override invocations across Codex processes. No warning was hidden, no hook trust was weakened, and no AO binary or provider configuration was replaced. These warnings remain a separate integration concern.
- The remote site remains **read-only**. No remote command backend was installed or enabled. Local guarded controls and remote operational details are the supported paths.
- Authenticated external browser login was **not exercised**. Origin rendering, current telemetry, Access configuration, tunnel status and unauthenticated denial were checked independently.
- A real emergency kill, real graceful shutdown, provider replacement, agent launch/restore, model instruction, and whole-machine reboot were **not performed merely to test a button**. Native behavior remains covered by the existing safe regression fixtures and prior operational evidence, not a new destructive acceptance run.
- Existing project PR/CI failures are application work, not dashboard failures; this audit does not declare those PRs accepted or merge them. Protected-main integration remains separate.

Run the regressions from the repository root:

```powershell
node tools/fleet_control/check-fleet.js
node tools/fleet_control/test-browser-controls-isolated.js
python -m pytest -q -p no:cacheprovider tests/unit/test_fleet_mission_runtime.py tests/unit/test_fleet_proxy.py tests/unit/test_mission_control.py --tb=short
git diff --check
```

When resuming work, refresh the local Mission Control/Fleet page, use **Startup**, read the preserved-target preview and confirm **STARTUP**. Wait for current runtime verification before using Sustained, Turbo, All Hands or Find Work. Startup intentionally leaves automatic dispatch paused until a separate explicit choice. Recheck is for uncertain interrupted operations, not permission to discard their durable records.
