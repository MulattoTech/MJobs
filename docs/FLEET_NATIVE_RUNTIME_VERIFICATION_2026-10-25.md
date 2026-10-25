# Native runtime recovery and readiness verification — 2026-10-25

## Scope and result

This follow-up to PRs #810 and #811 addresses the remaining Windows Startup blocker and slow readiness responses. It does not promote whole-product acceptance or authorize automatic model work. Canonical changes are in `tools/fleet_control`; runtime state stays outside Git.

AO v0.14.6 reports an unmeasured process count as `null`. Treating that as zero would be unsafe, but rejecting every null also made stopped preserved sessions impossible to resume. The new reader leaves native counts untouched and supplies independent, short-lived evidence that the exact native runtime is absent.

## Native evidence and submission rules

- The Windows helper reads two complete process inventories and stable ownership records. It checks executable paths, creation identity, native argument parsing, matching host ownership, and explicit session-owned provider processes.
- AO's run-file/PTY registry root (`~/.ao`) and chat data root (`~/.ao/data`) are distinct. The data directory is bound to the live daemon through its database marker, lock PID, run-file PID/port, executable and creation identity. A missing file in an unverified directory is not absence.
- Missing, unreadable, changed, malformed or ambiguous ownership remains unknown. Reused PIDs and unresolved registry entries block the proof.
- Evidence lasts at most ten seconds and belongs to the exact session identity. Restore repeats inspection immediately before mutation and after any native exit.
- Unknown counts never become healthy or numeric zero. Native absence never clears uncertain/inflight submissions. Only positively verified current runtime/controller health can reconcile an earlier submission.
- The helper is read-only. It does not start/stop processes, write ownership files, read credentials or transcripts, or connect to provider control sockets. Native AO operation and host locks remain the final concurrency authority.

## Dashboard responsiveness

Ordinary readiness polls wait at most one second for the shared inspection, then return explicit pending/not-ready status. Repeated refreshes do not spawn overlapping probes. Stale or changed-manifest results cannot be presented as current readiness. Forced Startup/Recheck observations retain their existing safety deadline and cancellation and require a post-request inspection. Controller state is read after the potentially slow process inspection.

## Verified local runtime recovery

The current primary's saved Chat provider handle disagreed with the active project conversation branch, which belonged to an older primary. This is a native AO precondition that returned HTTP 500 before Codex could resume.

The supported Chat-to-TUI interface transition with `policy: drain` and `historyPolicy: strict` completed for the existing primary. Its native Codex thread, workspace, branch and session ID were preserved; no new prompt was sent. A live native resume process and its exact launch generation were verified. The preserved fleet manifest's mode was updated with a backup. The verified primary was then exited through the native API, leaving the fleet paused for the user's Startup action. No AO database rewrite occurred.

Two obsolete PTY registry references were investigated; one had already been pruned and the remaining entry pointed at an unrelated reused Windows PID. The remaining entry was backed up and removed only while AO was stopped, followed by a normal native restart. The unrelated process was preserved. Twenty-one completed native reviewer surfaces were archived through the official API after confirming no active review jobs; their history/results were preserved.

## Executed validation

On the native Windows machine:

| Verification | Result |
| --- | --- |
| `node tools/fleet_control/check-fleet.js` | 32 groups PASS, zero failed, zero not-run, stable source |
| Windows runtime reader | 94 tests PASS, including the native fixture |
| Native PowerShell policy/parser fixture | 22 checks PASS; zero process mutations/ownership writes |
| Startup adapter | 167 tests PASS |
| Restore safety | 85 tests PASS |
| Startup integration | 60 tests PASS |
| Startup controller | 63 tests PASS |
| Live read-only native inspection | 29/29 preserved targets received valid absence evidence; raw counts stayed null; 2.23 seconds |
| Native primary handoff | Completed; original thread/workspace/branch verified; idle; subsequently stopped |

Tested Fleet source fingerprint: `39af37ab8d731642913a6aff6788d3ffbc3b1d15dee1fd7429557d2a0a4fd603`. GitHub gates must still be checked on the exact publication head before merging. Local source provenance is separate from hosted check success.

## Limits and operating state

Turbo and Sustained remain off. The fresh Codex allowance was 100%; the independent monitor acknowledged the 7% guard, with a 15% new-assignment reserve. Provider metering and in-flight work mean an exact final balance cannot be guaranteed. A 29-worker paid workload was not launched during maintenance. Browser click-through remains NOT_RUN because the connected browser was unavailable; native Windows launch, API, proxy and UI regression checks are separate evidence.

AGY's native model variants and observation-based usage remain supported; no standalone Gemini CLI installation or fabricated quota value is claimed. Unknown Google usage stays unknown until a fresh native observation.

Native contracts reviewed against [AO v0.14.6](https://github.com/OrchestratorInc/agent-orchestrator/tree/e424538c94ab67940b555efec4d46d25122edec0): `session_top.go`, `service/usage/memory.go`, `service/chat/service.go`, `session_manager/interface_transition.go`, `conpty/runtime.go`, `conpty/ptyregistry/registry.go`, `persistenthost/host.go` and `config/config.go`.
