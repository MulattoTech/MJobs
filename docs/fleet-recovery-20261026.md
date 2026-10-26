# Fleet recovery and remote status: 26 October 2026

## Delivery checkpoint

The canonical source is [MulattoTech/MJobs](https://github.com/MulattoTech/MJobs).
Fleet Control and Mission Control were already tracked under
[`tools/fleet_control`](../tools/fleet_control/README.md) and
[`tools/agent_dashboard`](../tools/agent_dashboard/). The inspected installation
matched main `3dde716311194f6f1fa9c892816818796d7652b2`, including
[PR #812](https://github.com/MulattoTech/MJobs/pull/812).

| Final acceptance observation | State at document preparation |
| --- | --- |
| Current healthy preserved runtimes / 29 | **29/29 verified** at 2026-10-26T02:28:09.596Z |
| Final Startup phase and unresolved native submissions | **`ready_paused`**, restore complete, **zero inflight / zero errors**; six earlier branch receipts and five distinct post-review receipts retained |
| Dispatch mode and enabled state | **Sustained, target 12**, enabled at 2026-10-26T02:28:36.763Z |
| Subsequent automatic assignment activity | **BLOCKED**: GitHub CLI authentication is invalid; no new assignments; desktop reauthentication required |
| Latest full Windows source regression run | **PASS: 37/37** at 2026-10-26T02:38:46.068Z; zero failed or unexecuted groups, source stable; includes the explicit planner-hold display |
| Installation validation | Final installed source passed **37/37** at the 02:39:27.867Z deployment checkpoint; completed restore ledger unchanged |
| Authenticated external remote login | **NOT EXERCISED**; origin rendering/status and unauthenticated Access redirects were verified |

This document records the diagnosed causes, implemented controls and observed
recovery. Restored runtimes, enabled dispatch and successful automatic assignment
are separate acceptance observations. The fleet is restored; the recorded
dispatch authentication failure requires a desktop sign-in before automatic
assignment can proceed. Record the published commit separately.

## What the indicators mean

| Indicator | Meaning |
| --- | --- |
| Usage 100% remaining | The provider reported unused allowance. |
| Stop guard armed | Automatic quota protection is configured. Work also requires a fresh acknowledgment of the exact settings. |
| X / 29 runtimes verified now | Native runtime and controller evidence currently matches preserved targets. |
| Dispatch paused | Automatic new assignments are disabled; already running agents can continue existing work. |

The board now keeps these facts separate. Current verification replaces an old
restore's progress when fresh evidence is available. Pending or stale inspections
cannot become a current readiness claim.

## Diagnosed failure sequence

1. **The original restore had failed before a fresh protected Startup.** The
   legacy attempt began on 25 October at 16:41 UTC. Its primary resume returned
   HTTP 500 three times, and workers were held until the primary became healthy.
   The earlier primary Chat identity problem was subsequently repaired, but no
   new protected Startup had run after that repair. Quota recovery and an armed
   stop guard did not resume that operation.

2. **A native inspection variable collided with PowerShell `$Host`.** The
   registry loop used `$host`; PowerShell treats variable names without regard
   to case, so assignment failed against the read-only automatic variable.
   An all-stopped probe had not exercised the loop with a live TUI host. Restoring
   the primary exposed the defect.

3. **Normal-user inspection could not read unrelated elevated provider
   processes.** The broad process inspection correctly withheld absence evidence
   when command lines were unavailable, but this also blocked remaining workers.
   The repair first attempts a read-only limited-information query bound to the
   same process creation. An opaque non-AO provider can be excluded only through
   complete, creation-ordered ancestry to an SCM-attested running LocalSystem
   service using canonical System32 `svchost.exe`. Unknown AO owners, missing or
   reused parents, cycles and changed service identity remain blocking. Both
   observations include the full ancestry and service identity in their hashes.

4. **`waiting_input` was treated as unavailable.** AO uses this state for an
   empty instruction prompt. It is distinct from `blocked`, which represents a
   pending tool or approval decision. Positive process/controller evidence now
   permits a waiting-input runtime to be healthy, and Sustained, Turbo and All
   Hands can select it. Each send rechecks activity at delivery; a transition to
   `blocked` prevents automatic input. See AO's pinned
   [activity contract](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/domain/activity.go).

5. **An interrupted partial restore had no safe continuation state.** Earlier
   reconciliation could recognize a fully healthy fleet or a run proved never
   submitted. A healthy primary with stopped workers fit neither outcome. The
   new read-only `resumable` outcome accounts for every target while retaining
   the existing restore ledger. It enables a separate, explicit Startup request.

6. **Preserved stopped targets became archived during sequential recovery.** At an
   intermediate 15/29 healthy observation, fourteen preserved workers were still
   awaiting restoration and had become `isTerminated: true`. AO's reaper suppresses
   dead observations when at least five are dead and they exceed half of all
   observations in that pass. As more runtimes recover, that suppression can
   cease, after which the lifecycle manager marks definitively dead sessions
   terminated. This source-supported mechanism is consistent with the observed
   archival; the evidence does not attribute each status write to a specific
   daemon call. The denominator is the reaper's observed set, not necessarily
   this 29-target manifest. See the pinned
   [reaper](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/observe/reaper/reaper.go)
   and [lifecycle manager](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/lifecycle/manager.go).
   `ao session top --all` omitted those archived targets. The previous evidence
   reader excluded all terminated identities, leaving the existing native
   `session restore` branch unreachable. The reaper remains enabled.

7. **Six registered worktrees had a different current branch from AO's saved
   metadata.** Read-only Git and native metadata inspection verified those
   existing worktrees and their current branches. Native restoration preserves
   the existing checkout and can update AO's branch metadata to that checkout.
   Broadly relaxing branch checks would hide unrelated changes. An explicit,
   reviewed mapping and a durable native-restore receipt instead permit only
   each verified transition. See AO's pinned
   [existing-worktree restoration](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/adapters/workspace/gitworktree/workspace.go)
   and [session restoration](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/session_manager/manager.go).

8. **The first integrated Startup exposed a missing bootstrap field.** The
   adapter returned the reviewed branch-map revision, but the controller's saved
   bootstrap allowlist omitted it. The mutation boundary rejected the mismatched
   handoff before starting a new restore. The earlier 35/35 fixture run had not
   exercised this full controller-to-adapter path with a nonempty reviewed map.
   The correction preserves and validates that revision through the durable
   controller state and its mutation context. A regression exercises the real
   controller and adapter with a reviewed map, reproduces the earlier omission,
   and verifies the safe continuation path. The integrated Windows suite passed
   after this correction.

9. **A legitimate auxiliary terminal lacked an agent launch ID.** After the
   daemon returned, an AO `shellterm-` host was present in the native registry.
   The evidence reader incorrectly required an agent-generation `launchId` on
   every entry. AO's shell service uses a separate ID namespace and its registry
   schema deliberately omits an empty launch ID. The repair accepts only the
   exact native auxiliary shape, generated ID, option order, approved executable,
   host PID/creation, live daemon parent, registration time and bounded loopback
   address. Unknown entries and unresolved reservations still block evidence;
   all original target and two-observation stability checks remain. See the
   pinned [shell service](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/service/shellterm/service.go)
   and [registry schema](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/adapters/runtime/conpty/ptyregistry/registry.go).

10. **Five later native restores adopted additional existing worktree branches.**
    After the unexpected reboot, five targets that had previously been healthy
    were archived while sequential restoration continued. Their native restore
    commands succeeded, retained the original conversations and worktrees, and
    adopted the existing checkout branches. These five transitions had no
    pre-operation branch approval. The controller correctly retained their
    submission fences when the saved branch no longer matched. A distinct
    post-submission review now permits explicit completion only after proving
    the exact successful commands and their current preserved outcomes. It does
    not manufacture an earlier approval or repeat native restoration.

## Operational interruption during recovery

Windows recorded an unexpected shutdown at **2026-10-26T01:13:27Z** and a new
boot at **2026-10-26T01:14:29.500Z**. Event 6008, Kernel-General event 12 and
`LastBootUpTime` corroborate the interruption. The evidence does not establish
its initiating cause. All fifteen previously observed AO runtimes were gone
afterward; the quota guard had not tripped. The recovery work did not request a
reboot or make OS, driver or firmware changes.

The new dashboard and remote-status sign-in tasks returned automatically, and
the source, preserved manifest and restore ledger survived. The **01:16:36.618Z
35/35 Windows test result was obtained after this reboot**. Native recovery then
continued through the Limited broker, fresh quota protection and explicit
Recheck. A stable Limited probe established absence for all 29 exact targets
without process or ownership changes. The failed-start Recheck then returned
`resumable` while leaving the older restore ledger byte-for-byte unchanged, and
a new protected Startup was accepted. No stale runtime count was retained as a
current-health claim. The later accepted completion and fresh Recheck verified
29/29 runtimes with no unresolved submissions. Dispatch authentication remains
a separate operational blocker below.

## Dispatch authentication checkpoint

After restoration, Sustained dispatch was enabled with target 12 at
**2026-10-26T02:28:36.763Z**. Its first tick received **HTTP 401 from the GitHub
CLI**, so it submitted no new assignments. Both the elevated and Limited user
contexts reported invalid CLI authentication for the configured account. The
Limited context was checked again at **2026-10-26T02:36:08.538Z** using the same
desktop profile, with no alternate GitHub configuration or host override. The
installed CLI was 2.103.2. Git Credential Manager had no reusable sign-in;
anonymous repository reads did not establish authenticated account permissions.
The user must reauthenticate the desktop GitHub CLI.

This is separate from runtime restoration: **29/29 remained verified** when
restoration completed. An enabled dispatch toggle does not establish successful
work selection. Verify GitHub read access from the actual Limited dashboard
context, then observe a subsequent successful dispatcher tick and its resulting
activity before declaring automatic dispatch healthy.

In a normal, non-elevated PowerShell window under the desktop account used by
the dashboard, complete the browser sign-in and check the resulting account:

```powershell
gh auth login --hostname github.com --web
gh auth status --hostname github.com
```

These are GitHub's documented [browser login](https://cli.github.com/manual/gh_auth_login)
and [authentication status](https://cli.github.com/manual/gh_auth_status) commands.
The status check must report a valid active account. Then verify the board's
GitHub inventory refresh and subsequent automatic assignment. The connected
ChatGPT GitHub app is a separate authentication context; publication through
that connector does not sign the desktop dispatcher in.

The final presentation correction reports a running scheduler's actual planner
hold in local health, even while its dispatch toggle is enabled. The remote
projection maps the exact known GitHub hold to **"GitHub repository inventory
is unavailable or stale"**. Unknown hold text becomes a generic message, so raw
errors or credentials cannot enter the status export. A recovered planner removes
the display hold only when its observed state changes.

## Preservation and recovery guarantees

### Partial reconciliation

**Recheck safely** launches no agents. `resumable` requires inactive restore and
launch operations, paused dispatch, no graceful drain, an unlatched emergency
state, fresh allowance above 15%, and acknowledgment of the enabled 7% /
five-second / lowest-window guard. The preserved manifest, restore source,
desired IDs and recorded operation must agree.

Every exact target must be healthy or positively stopped, without conflicting
controllers or unresolved durable native submissions. Missing evidence cannot
stand in for zero. The restore ledger must remain unchanged during inspection;
the controller also validates the proof's lists, counts and operation binding.
Recheck retains the ledger and records its healthy/stopped partition. Ordinary
dispatch stays blocked in `resumable`; a **new Startup** revalidates the full
safety sequence before submitting further restoration.

A start rejected before RestoreFleet accepts it has a separate, narrow proof.
The durable older restore record must be valid, unchanged in raw bytes and parsed
state throughout inspection, and equal to the in-memory ledger. Its start must
strictly predate the failed Startup. Exact stage markers must show only the
attempt to start RestoreFleet, without a committed restore receipt or a later
restoring stage. A prior owner must differ, or match the explicitly supported
legacy source shape. This relies on RestoreFleet persisting its new operation
before scheduling any native work. It also requires the full fresh target
partition, guard acknowledgment and empty unresolved-submission set. Both
adapter and controller validate the proof. Recheck records `resumable` and leaves
the older restore ledger untouched; an error message or `restoreStarted: false`
alone cannot authorize another attempt.

### Archived preserved targets

The terminated-session exception is restricted to **TUI targets explicitly
preserved as eligible and nonterminated in the original manifest**. Both the
evidence producer and consumer require that scope. It binds ID, project, kind,
harness, model, mode, branch and snapshot time. New snapshots also pin creation
time; legacy snapshots require native creation to predate preservation and remain
unchanged across the probe. Historical archives outside the manifest and archived
Chat sessions remain ineligible for this exception.

The mutation boundary rechecks the saved manifest, source and desired IDs. Even
a measured zero cannot authorize an archived restore without scoped native
absence. Native ownership follows AO's
[fenced runtime contract](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/adapters/runtime/conpty/runtime.go).
No count is manufactured and no native submission is blindly repeated.

### Reviewed branch adoption

`restore-branch-adoptions.json` is a **local runtime record**, not repository
configuration. It binds the original manifest's exact SHA-256 bytes and timestamp,
review time/reference, original target identity and native creation, reviewed
existing branch, workspace path and repository path. Its byte revision is bound
by a new Startup and retained in the restore source.

Before a mapped native restore, the official loopback workspace endpoint and
read-only Git commands verify the same existing, registered worktree, repository
and reviewed branch. Native absence is refreshed afterward. The native `restore`
reservation and its branch receipt are persisted together. Workspace verification
runs again after restoration. Acceptance requires the exact reviewed branch,
same creation identity and positive current runtime health. Asynchronous proof
also rechecks approval, source, receipt and submission-fence identity.

A reviewed file alone cannot adopt a branch. An uncertain native result remains
fenced; read-only observation preserves the ledger, while a restore tick can
record verified health through the normal completion path. Only already verified
receipts can be carried into a later explicit run, with their original provenance
and an immutable digest. A valid one-time mapping becomes inert for a strictly
newer valid snapshot, with a diagnostic; same-generation byte changes, future
records and malformed files stay blocking.

The recovery preserves the frozen manifest, session identities and worktrees.
It performs no checkout, reset or native database rewrite. Before live restoration,
the fourteen pending targets' registered worktrees and native history files were
checked, and untracked work was backed up locally. After the unexpected reboot,
read-only checks also found all 28 TUI targets' recorded native conversation IDs,
history files and workspace paths intact. At **2026-10-26T01:50:30Z**, a live
inspection verified all 28 original conversation UUIDs in native Codex resume
commands and their preserved workspaces. Five additional branch/worktree checks
passed at **01:51:18Z**; their reflog entries predated the restoration commands.
Four dirty files across those targets were backed up byte-for-byte, without
editing the worktrees. History-file availability alone is preflight evidence;
the resumed conversation must also be verified live. Native restoration may
otherwise fall back to a fresh provider conversation when history
is unavailable; see the pinned
[native Codex history check](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/adapters/agent/codex/codex.go)
and `restoreArgv` in the
[session manager](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/internal/session_manager/manager.go).
Private paths, reviewed mappings, history, logs and runtime
receipts must remain outside Git.

### Explicit review after a successful native command

`restore-post-submission-review.json` is separate from the original branch
approval. It records that review occurred **after** native submission succeeded.
It binds the frozen manifest bytes, current Startup run, restore source and
desired IDs, raw paused ledger, exact unresolved fences, successful audit-line
hashes, original native session creation, expected existing branches, workspace
paths and native conversation/process identities. A pinned history-evidence
file connects the original conversation IDs and workspaces to the current
resumed processes.

The original pre-start capture lacked an internal observation timestamp. Its
provenance therefore explicitly says `filesystem-last-write-time`, retaining
the original file hash, unchanged modification time and review reference. The
verified file modification time, **2026-10-26T01:29:19.720Z**, preceded this
Startup. The consumer checks the original bytes and metadata in a contained,
non-symlink path. It does not invent a historical `checkedAt` field.

The local-only `/api/master/restore/complete-reviewed` action requires the exact
review revision, current run and `COMPLETE REVIEWED RESTORE` confirmation. It
holds the existing mutation lane, checks durable Startup and restore ownership,
requires settled operations and fresh quota protection, and freshly verifies
every preserved runtime. Each reviewed target also receives a read-only Git
workspace proof and two stable process observations binding its original
conversation UUID, exact Codex PID/creation/executable and AO host ancestry.
The native argv parser permits the installed Codex `resume` option grammar
before the sole final UUID; it does not mistake option values for a conversation
ID. AO's pinned [Codex command builder](https://github.com/OrchestratorInc/agent-orchestrator/blob/e424538c94ab67940b555efec4d46d25122edec0/backend/pkg/agentruntime/command.go)
places these options before the identity.

All evidence and ownership bindings are checked again before a single atomic
ledger write. Only the reviewed fences are removed. Distinct completion
receipts retain the original fence, prior error, native success audit and honest
history provenance; the earlier six approval records remain unchanged. An
atomic-write failure leaves the original live and durable ledger intact. A
review file alone cannot change readiness. No provider command, checkout,
manifest rewrite or automatic dispatch occurs. A subsequent explicit Recheck
must still reach `ready_paused`. Snapshot capture remains frozen while Startup
requires reconciliation, and a strictly newer valid snapshot later makes the
one-time review inert without erasing its historical receipts.

## Installation and operator runbook

The installed source lives in `C:\Tools\AO-Fleet-Control`; the repository is
`C:\Tools\Dev\MJobs`. Pause dispatch and restoration before replacing reviewed
source, preserve runtime state, and restart intentionally. Do not delete a ledger
or rewrite a manifest to make a retry eligible.

Register or refresh the hidden dashboard task and open the board:

```powershell
pwsh -NoProfile -File C:\Tools\AO-Fleet-Control\Install-Fleet-Dashboard-Task.ps1 -RunNow
pwsh -NoProfile -File C:\Tools\AO-Fleet-Control\Start-Fleet-Control.ps1 -OpenBrowser
```

**MJobs Fleet - Dashboards Standard** runs at sign-in with an
Interactive/Limited principal. Its hidden wrapper starts dashboards, verifies
health and serializes concurrent launches. It does not restore agents or enable
dispatch. A dashboard restart pauses saved restoration instead of replaying it.

1. Open [Mission Control](http://127.0.0.1:18729/) and its
   [Fleet panel](http://127.0.0.1:18729/fleet/); the direct fallback is
   [Fleet Control](http://127.0.0.1:8787/).
2. Inspect Startup, current verified runtime count, guard freshness and dispatch
   separately. Preserve `startup-state.json`, `restore-state.json`,
   `restore-snapshot.json`, launch receipts and dispatch reservations locally.
3. If a reviewed branch transition is necessary, verify the existing worktree and
   history first and install the strictly bound local review record. Its presence
   during the old run's Recheck grants no branch adoption without a native receipt.
4. For an interrupted run, use **Recheck safely**. If it reports `resumable`, use
   **Continue Startup…** with a new request. Unknown ownership, changed identities,
   outstanding submissions or stale protection remain blocking.
   If native restore already succeeded but a verified existing branch transition
   prevents acceptance, retain every fence and use the distinct reviewed
   completion procedure above. Do not resubmit the native command or reinterpret
   a post-operation review as pre-approval.
5. Confirm Startup and wait for every expected runtime and primary controller to
   be verified now. Completion is `ready_paused`. Existing agent work can resume
   and consume allowance before automatic new assignments are enabled.
6. Enable the intended dispatch mode separately. This incident's intended setting
   was **Sustained, target 12**. Verify the actual enabled state and subsequent
   activity. Sustained and Turbo remain mutually exclusive.

Read-only diagnostics include `/api/startup`, `/api/state`, `/api/master`,
`/api/quota` and `/api/health` on port 8787, or under `/fleet/api/` through Mission
Control. Readiness and dispatch should be checked after source restart and again
after any explicit continuation.

## Remote status on M-TechLabs.com

The remote view is [M-TechLabs Mission Control](https://m-techlabs.com/mission-control/)
on the existing Access-protected hub. The repair did not independently verify
the Access application's allowed-user policy through the Cloudflare API.
The implementation is in
[`tools/fleet_control/remote`](../tools/fleet_control/remote/README.md).

Verify Access redirects or denies unauthenticated HTML, assets and JSON, then
install the assets, hub card and publisher task on the existing protected hub:

```powershell
pwsh -NoProfile -File C:\Tools\AO-Fleet-Control\Install-Fleet-Remote-View.ps1 -InstallTask -RunNow
```

The installer preserves a hub backup. **MJobs Fleet - Remote Status Standard** is
an Interactive/Limited sign-in task, without a runtime time limit. Its singleton
mutex and `IgnoreNew` task setting prevent duplicate publishers.

This is a one-way status publisher. Six fixed loopback GET sources are reduced to
an allowlisted projection, then atomically written to
`site/mission-control/status.json` every ten seconds. Browser requests never reach
the local control planes. Tokens, prompts, local paths, raw errors, logs and
process trees are excluded. The local launch, send, restore, kill and configuration
routes retain their loopback/origin restrictions.

The page shows verified runtimes, reported activity, quota protection, Startup,
dispatch and Mission Control lanes separately. It refreshes every ten seconds
and checks freshness every second. Both publication age and individual runtime /
quota sample ages expire after 45 seconds. A pending master inspection receives
at most two additional GETs, 250 ms apart, within the same eight-second deadline;
an unresolved inspection stays unknown. The PC, publisher and Docker service hub
must be running for fresh remote telemetry. Authentication and an advancing
snapshot must be verified separately from an unauthenticated Access redirect.
Origin rendering/status and redirects were checked during this repair; an
authenticated external login was not exercised. The hub directory itself is not
a Git repository. Its reproducible monitor source and installers are tracked in
MJobs.
After the reboot, the remote task's sign-in launch and an advancing origin
snapshot were also verified; the page correctly showed Startup in progress and
dispatch blocked. This origin check does not substitute for external login.

## Validation and publication

Intermediate checks established the following; none substitutes for the final
integrated source fingerprint and live outcome:

- Windows native runtime evidence: **120 passing tests**, including the scoped
  archived-target cases. A live read-only probe accounted for fifteen positive
  native runtimes and fourteen archived targets with exact absence evidence.
- Workspace verifier: **37 passing Windows tests**, plus successful production
  API/Git verification of all six reviewed worktree mappings.
- Branch approval and receipt validation: **63 passing fixture checks**.
- Nine affected source/test file suites passed after the final immutable receipt
  race fix. Native agent review found no remaining high-confidence blocker.
- After the handoff correction and strict uncommitted-start recovery were
  integrated, the full Windows runner passed **35/35** at
  **2026-10-26T01:16:36.618Z**. The adapter suite included **273 passing tests**,
  including the real controller-to-adapter revision handoff and preservation of
  the older durable restore ledger during explicit Recheck.
- After the auxiliary-shell correction, the native helper passed **100 checks**
  with no process mutations or ownership writes. Both the Windows worktree and
  installed 35-group suites passed. The current worktree result was recorded at
  **2026-10-26T01:32:41.173Z**.
- The new native-conversation verifier passed **31/31 Windows fixtures**,
  including 30 PowerShell assertions. A Limited live probe verified all five
  reviewed runtimes twice at **2026-10-26T01:55:41.032Z**, without mutations.
- The real post-submission completion/control/Startup suite passed **43/43**
  locally. The Startup route/integration suite passed **63/63**. Cases include
  changed or missing evidence, altered source/fence/guard/generation during
  inspection, duplicate successful native commands, failed atomic persistence,
  separate earlier approvals, snapshot freezing and Recheck without replay.
  These checks supplement the final Windows integration run below.
- The complete **37-group Windows runner passed 37/37** at
  **2026-10-26T02:26:50.459Z**, with zero failed groups, zero unexecuted groups and
  `sourceStable: true`. At **02:26:50.548Z**, validation of the concrete five-entry
  review record also passed: its hashes, source, original history evidence and
  successful native audit records matched, and the paused ledger remained
  unchanged.
- The tested source was installed at **2026-10-26T02:27:31.557Z** using an owned
  backup and Fleet server restart, with preserved runtime evidence unchanged.
  The installed **37/37** gate passed. Explicit reviewed completion and Recheck
  then verified **29/29** at **02:28:09.596Z**, with restore complete, Startup
  `ready_paused`, zero inflight submissions and zero errors. All six earlier
  branch receipts and five distinct post-review receipts were retained.
- After the final planner-hold presentation correction, the full Windows source
  runner passed **37/37** at **2026-10-26T02:38:46.068Z**, with zero failed or
  unexecuted groups and `sourceStable: true`. The exporter suite now contains
  **15 passing checks**, including exact allowlisted GitHub holds and generic
  handling of arbitrary errors. The installed 37/37 gate also passed before the
  02:39:27.867Z deployment checkpoint, with the completed restore ledger unchanged.
- Final read-only UI checks passed on the local board and on the remote monitor
  at desktop and phone widths: 29 rows, 29/29 verified, 7% protection armed,
  100% allowance and the GitHub dispatch hold visible. Both views reported zero
  JavaScript exceptions and no horizontal overflow. The remote snapshot at
  **02:40:38.821Z** was current and showed Sustained enabled with dispatch blocked;
  no controls were clicked.

The completed 37-group Windows run recorded this source fingerprint:

```text
0a6cc3ce0546f03110e5069d0c27987a4e2b620ba31e94743016e373d0f51fef
```

The final allowlisted Windows runner contains **37 groups**, including the remote
exporter, branch suites and both post-submission suites. Run from the repository root:

```powershell
node tools/fleet_control/check-fleet.js
git diff --check
```

Retain `tools/fleet_control/diagnostics/preflight-results.json`, its
`sourceFingerprint`, and `sourceStable: true`. The fingerprint covers local and
remote JavaScript, PowerShell, VBS, HTML and CSS source. Fixture checks use no live
inference. The existing **Fleet / Windows control regressions** workflow uses
Node 24 and the same runner. The restored fleet and enabled dispatch were
observed independently of these source checks; successful assignment remains
blocked by the recorded GitHub CLI authentication failure. Keep the
authenticated external-login limitation explicit unless that check is actually
performed.

Publish through a focused feature branch and protected PR under
[`AGENTS.md`](../AGENTS.md) and
[`PR_PUBLICATION_POLICY.md`](PR_PUBLICATION_POLICY.md). Run the live queue preflight
with actual candidate evidence, then the commit provenance validator. Required
main checks remain **CI / required**, **Integration / PostgreSQL and Redis**,
**Disposable Compose / mock end-to-end**, **Dependencies / introduced
vulnerabilities**, and **Security / secret scan**. Hosted results must match the
published head; pending or skipped checks are not a pass.
