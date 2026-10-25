# Fleet startup and non-admin launch verification — 2026-10-25

This maintenance change repairs reset recovery and launch behavior on MulattoTechBox. It does not change MJobs requirement acceptance or claim a completed development sprint.

## Behavior

- Protected Startup validates the preserved manifest and fresh Codex allowance, holds automatic dispatch, releases only the inspected emergency-latch revision, verifies the old watcher has stopped, arms the 7% guard, waits for the monitor to acknowledge those exact settings, and restores the primary before workers.
- Current native chat-controller health is required. A saved "idle" label and wrapper processes are insufficient when the conversation endpoint reports a stopped controller.
- Durable request identities prevent double submission. Cancel, timeout, restart and late-result paths preserve uncertain operations. Explicit Recheck may recognize verified completion; it cannot blindly replay a resume or discard the original submission ledger.
- Startup leaves Turbo and Sustained disabled. Separate Open Orchestrator uses a fixed Limited/Interactive task and verifies the actual process token; it reuses an existing healthy daemon/UI.
- The same guarded shell module serves Windows PowerShell 5.1 and PowerShell 7. PS5 delegates into PS7 in its existing console. Profile installation preserves original text, makes exact-byte backups, and is idempotent.
- Mission Control exposes the controls at http://127.0.0.1:18729/fleet/ through its existing fixed-target loopback proxy. Direct fallback is http://127.0.0.1:8787/.

## Executed validation

| Check | Result |
|---|---|
| node tools/fleet_control/check-fleet.js | 31 suites passed; no failures or skipped suites; source stable during run |
| Startup controller | 63 cases passed |
| Startup adapter | 124 cases passed |
| Startup API/UI integration | 60 cases passed |
| Restore/controller recovery | 35 cases passed |
| Standard-user launcher | 36 Node fixture cases and 9 native PowerShell checks passed |
| Shared shell and profile installer | 218 native assertions across PS5.1 and PS7; temporary profiles only |
| pytest tests/unit/test_fleet_proxy.py tests/unit/test_fleet_startup_proxy.py tests/unit/test_mission_control.py -q | 36 tests and 31 subtests passed |
| Install-Orchestrator-Launcher.ps1 -Root C:\\Tools\\AO-Fleet-Control -WhatIf | Existing Limited/Interactive task verified; no task started |
| git diff --check | Passed |

The source commit and hosted results are recorded by the associated PR. These maintenance results are not whole-product acceptance evidence.

## Native observations

- Actual PS5.1 frontend token: medium integrity (8192), not elevated. The installed common module reached Codex Ready with one terminal window and one tab; the 45-second observation captured no additional visible console windows.
- PS7 baseline: medium frontend token, Ready, one terminal window and one tab. A fresh PS7 profile subsequently resolved codex from Codex-Shell and reported stable CLI 0.164.0.
- Desktop shortcut launch: one related app window and zero visible console events in 50 seconds under a medium token. The desktop package changed from 26.1016.8622.0 to 26.1021.3705.0 during its launch/update cycle. Its bundled CLI is separate from the npm CLI.
- Native AO daemon and desktop UI both verified with medium integrity (8192). The daemon answered the sessions API. Opening the UI reused the existing verified instance.
- Codex allowance was freshly reported at 100%; the 7% guard was armed and acknowledged. The new-assignment reserve remains 15%.
- Empty launch probes submitted no model prompts. The saved fleet was not resumed for testing. Chat controllers remained stopped, and both automatic dispatch modes stayed disabled.
- The independent RDC recovery task/watchdog remained available during maintenance; no broad kill of Codex or RDC was used.

Local sanitized evidence remains in C:\\Tools\\AO-Fleet-Control\\diagnostics, including fixed-ps5-verification-20261025.json, window-fixed-ps5-20261025.json, window-desktop-before-20261025.json, standard-daemon-verified-20261025.json, standard-ui-verified-20261025.json, and startup-final-fleet-tests-20261025.log. Runtime state, credentials, logs and backups are excluded from Git.

## Boundaries

The full 29-session paid fleet startup was not run during maintenance. Native launcher/controller checks and mocked recovery tests do not establish that every future provider operation will succeed. Provider metering delays and in-flight work prevent a guarantee of an exact 7% final billing balance.

The connected browser timed out, so there is no claim of a live visual browser click-through. API/proxy, DOM behavior, native Windows launch, cancellation and refresh checks were exercised separately.

AGY quota remains unavailable until its native client emits a fresh observation; standalone Gemini CLI was not detected. Unknown usage is not represented as 0% or 100%. The Codex guard is not a verified Google quota cutoff.

## Follow-up: current CI versus historical PR results

A live paused-fleet check found six agents flagged for failing CI although all of their tracked PRs were already merged. Completed PRs now retain their recorded results as explicitly labeled historical CI, without blocking current worker availability. Unknown/current failures and standalone reported failures remain conservative, and exited workers remain ineligible for dispatch. Twelve added scenarios execute the actual session transform and Turbo planner; the native Windows dashboard, Sustain, and Startup API/UI suites passed. The old prompt claim about six modified main files was replaced by a general instruction to preserve user edits.
