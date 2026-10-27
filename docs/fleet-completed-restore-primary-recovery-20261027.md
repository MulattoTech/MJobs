# Completed-restore reconciliation and primary recovery — 27 October 2026

## Observed failure

The user's Startup run `4d5100d7-16a5-44f9-a5a5-f8ff1ef76028` restored the recorded fleet at 06:03:35 UTC. Its primary terminal provider exited about five seconds later. Current-readiness verification correctly refused to call that a healthy fleet, but Recheck had no recovery branch for a completed restoration followed by a positively verified runtime stop. It returned the generic unknown-outcome message and left Startup inaccessible.

A targeted regression reproduced this missing case. The repair adds a `completed-restore` reconciliation binding. It requires the exact operation owner, ordered start/completion/verification timestamps, a ready receipt for every original target, matching durable and in-memory restore records, current health-or-stop evidence for all targets, no unresolved native submission, paused dispatch and the current acknowledged quota guard. The read-only recheck preserves the restore ledger and permits a new explicit Startup; it neither launches agents nor erases an unknown operation.

Twelve focused regression cases pass, including wrong owner, missing/future completion, missing or nonready target receipts, pending/uncertain native submission, disarmed guard, absent runtime proof and a durable/in-memory mismatch. The complete source and installed Fleet runners each passed **47/47 groups**. The tested source fingerprint is `f035eeeb4334f8308d6021a8ab0a4b2b904e7b2771c770d846a28b60c55b939d`. Deployment at **06:33:31 UTC** byte-compared all 138 canonical files and preserved the existing control-state files.

## Live recovery, not only fixture results

At **06:34:38 UTC**, the user's actual Recheck succeeded: it accounted for 30 healthy runtimes and the exact stopped primary, cleared the reconciliation requirement through the normal guarded API, and returned `resumable` without launching anything.

A new protected Startup was then exercised. The old primary's terminal resume again exited after briefly appearing online. That run was cancelled through the existing control API and safely reconciled; it was not repeatedly replayed.

The original Codex conversation could be read and loaded by Codex's backend without submitting any model turn. The separate native AO terminal-to-Chat handoff, however, failed with `TARGET_HISTORY_UNTRUSTED_TEXT_MISMATCH` after attempting strict history verification. This establishes that AO's handoff records disagreed; it does not establish a precise low-level cause for every terminal exit. No history verification, account security or approval policy was relaxed.

## Replacement primary with preserved original evidence

The user's broad repair authorization was fulfilled by replacing only the failed primary control session:

- Old primary `mjobs-luna-36` was archived through AO, not silently erased. Its native transcript and complete working directory were backed up first. The working-directory archive contains 2,602 files and was checked by CRC and per-file SHA-256; the native transcript copy was hash-verified. Existing artifacts were not requested for deletion.
- New primary `mjobs-luna-40`, named **MJobs Primary - Native Chat Recovery**, uses AO's native **Chat** controller with the same model and `auto` permission policy. It has a new provider conversation and an explicit local handoff. This is not represented as an exact continuation of the old private conversation.
- All **30 worker identities, original creation times, models, interfaces and terminal generations were verified unchanged**. No extra worker fleet was created or restarted.
- The replacement was positively verified as the sole active primary before adoption into the saved fleet. The manifest now contains 31 supported live targets. A supersession record preserves the old/new identity relationship; the prior Startup ledger was not manually cleared.

The first native creation request was rejected with a documented HTTP 400: AO supports `clientRequestId` only for workers, not orchestrators. No session existed for that request. A separately recorded one-shot primary creation omitted that unsupported key; the response and subsequent native session were verified before proceeding. No uncertain creation was blindly repeated.

The private recovery material is under the workstation's Fleet Control `backups` directory: `primary-native-interface-20261027T063737Z`, `primary-rebuild-20261027T064211Z`, and `primary-adoption-2026-10-27T06-48-39-220Z`. These private copies and native conversation content are deliberately excluded from Git.

## Successful startup and operating checkpoint

Protected Startup run `5a7b7eec-744f-4a09-adab-f248eac716d5` completed at **06:48:58.102 UTC** with `ready_dispatching`, no error, no reconciliation requirement and current **31/31** runtime verification. A real primary assignment was recorded at 06:49:00.725 UTC and native activity was observed. The pause/resume control APIs were exercised without killing active turns; Sustained dispatch was left enabled.

At **06:54:41 UTC**, the new primary was still healthy and busy, 31 runtimes remained verified, and two native agent turns were executing. The quota report was fresh at 56%, with the acknowledged 7% stop guard unchanged. This is an observed checkpoint, not a guarantee of permanent 100% utilization or acceptance of every generated artifact.

The saved policy remains a target of 31 concurrent runtimes, zero reserve workers and evidence-backed productive discovery. The 30-second scheduler, resource pacing, duplicate-delivery protection, 15% new-work brake and 7% stop guard remain. Existing PR/CI ownership and protected integration rules remain separate from runtime readiness.

## UI and reporting verification

The isolated real-browser button suite passed **38 scenarios**, using 35 inert fixture requests and zero native agent commands. Live Recheck and Startup were executed separately as described above. The local browser was checked against actual Fleet Control on Mission Control's shared port; the live roster contains 31 rows and the new primary instead of the archived original. The normal remote status feed reports the current healthy fleet and enabled dispatch.

The independent analytics collector was found stopped, so it was started through its existing Limited scheduled task. Fresh Atlas data then included all 31 current agents and the replacement primary. No old telemetry cache was deleted or rewritten to manufacture freshness. Remote dashboards remain read-only behind their existing Access policy; authenticated external login was not independently exercised.

## Verification commands

```powershell
node tools/fleet_control/check-fleet.js
node --test --test-name-pattern='completed restore followed|completed-operation recovery' tools/fleet_control/test-startup-adapter.js
node tools/fleet_control/test-browser-controls-isolated.js
git diff --check
```

The runtime fleet was left enabled, not awaiting another user Start click. A browser hard refresh loads the installed scripts; current readiness and actual native activity, not an old successful run or armed guard alone, are the operating evidence. Native destructive controls were not executed simply for a button test. Publication uses the existing PR #825 and preserves draft state, normal checks and protected main.

## Final live browser and display correction

The historical completed-restore branch in the master badge could mask enabled dispatch with a stopped-style FLEET RESTORED label. The branch is now restricted to actually paused dispatch. Fresh healthy enabled dispatch displays PRODUCTION ACTIVE; a genuine unverified runtime still displays DISPATCH HELD. The added unit test failed before this one-condition change and passed after it.

The final source and installed runners again passed **47/47 groups**. Final canonical source fingerprint: `afdc52e30d8684e1a20fa02ab0e9fad5128d32625aab7f3ba56d6bac14d050cb`. The canonical source bytes match the installed code; the installed directory also contains private diagnostic helpers and therefore has a distinct whole-directory fingerprint.

At **2026-10-27T07:07:12.130Z**, an actual browser on the local shared Mission Control port opened all **31 agent detail panels**, displayed the replacement primary and excluded the archived original, refreshed Startup, clicked **Pause**, verified dispatch stopped, clicked **Start sustained mode**, verified its confirmation was required, and confirmed **Resume**. It observed PRODUCTION ACTIVE afterward. The only browser mutations were `["/fleet/api/master/pause", "/fleet/api/sustain/toggle"]`. There were zero JavaScript exceptions, and the Startup run identity was unchanged. This is live control evidence, separate from the isolated38-scenario suite.

The headless acceptance check explicitly keeps its tab visible so the application's intentional background-tab polling suspension does not invalidate the test. A prior background-tab test was not counted as a pass; its verified prior enabled-dispatch state was restored before continuing. The completed live check left automatic dispatch enabled.

## Concurrent reviewed fixes preserved

Before publication, the existing PR advanced to `d5427a364f8d841ad7fc09c08cc7ae769586b48d` with four nonoverlapping files. The six-file repair was backed up and hash-checked, the branch fast-forwarded, and all local repair bytes verified unchanged. The additional pause fences and stricter terminal-delivery evidence classification were retained, not overwritten. The combined source and installed runners again passed47/47.

Combined source fingerprint: `14b7021d697fc9cf602a7faaa77e915ec8a998e1fcbd61ca98d4eb5eb5bd4065`. Dashboard-only deployment completed and Sustained dispatch was re-enabled at **2026-10-27T07:13:50.4504632Z** after current31/31 readiness. No native worker was restarted. The earlier native recovery, browser pause/resume and primary-preservation receipts remain observed evidence for their stated timestamps, not promises about untested future conditions.
