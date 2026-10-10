# MJobs durable run / worker recovery sprint - 2026-10-10

## Checkpoint 1: executable product work

Source commit: `fcc292652b08a4a53a88744693055b7b7c1bbaf5`. Source tree: `457a1523897349cbc41cec94dd29a91b5b4fff24`.
Base: `f399e00da63c7bf14cd5281b71bba5daf8d62e10`.
Branch: `chatgpt/worker-run-identity-20261010`.
Worktree: `C:/Tools/Dev/MJobs-WebWorker-20261010`.
Issues #146 / MJ-REQ-145 and #139 / MJ-REQ-138 remain OPEN/PARTIAL.

The connected mock controls now preserve caller intent before sending it; API admission snapshots immutable inputs; worker generations share one physical attempt ID. A lost acknowledgement can be recovered with GET, cancellation remains terminal, and a new reviewed run requires a new explicit action. RUNNING attempts are never stolen. Ambiguous submission is reconciled by receipt, never blind resubmission.

Exact evidence:
- `docs/evidence/2026-10-10-worker-run-identity.json`
- `docs/evidence/2026-10-10-run-recovery-controls.json`

Local checkpoint: **80 Python tests run: 79 passed, 0 failures/errors, 1 skipped** (unconfigured isolated PostgreSQL recovery database). **586/586 frontend tests passed across 101 files**; TypeScript/Vite build passed. There are 29 new Python scenarios (26 backend and 3 connected UI) and 19 new frontend tests, all passing. Counts from repeated/subset runs are not additive.

Actual worker exit, database reopen, immutable cancelled attempt, lost receipt, lost UI acknowledgement, rapid duplicate clicks, and exactly one fictional submission are exercised. Real ATS, PostgreSQL and new Redis/multi-host acceptance are not claimed. Existing Node supported-engine and large-JS-chunk warnings remain; no host/runtime version or safety setting was changed.

## Ownership and continuation

Local exclusive claims: `codex:webpro-worker-identity-20261010` and `codex:webpro-run-recovery-ui-20261010`. Integration lock acquired by this session at `2026-10-10T14:22:31.735636Z`; recheck exact current ownership, never infer it from this historical receipt. Other agents and their 48 pre-existing claim records were preserved. A Desktop Commander idle-backend file handle initially prevented atomic claim writes; its existing supervisor restarted that backend, after which the normal claim command succeeded. No coding agent, MJobs runtime, or host security control was stopped/modified.

Canonical acceptance at entry: MJ-REQ **102 PASS / 101 PARTIAL / 163 NOT_RUN (366)**; MJ-REF **19 PASS / 4 PARTIAL / 36 NOT_RUN (59)**. These denominators must remain separate. Canonical tracker update is deferred to the accepted integration batch; no requirement is promoted by this checkpoint alone.

Checkpoint 1 is committed locally; publication and main integration are recorded by the subsequent issue receipt/final closeout. Primary main and personal app runtime have not been changed at this checkpoint.

Next dependency-ready slice: close the legacy manual-auth rerun path's terminal-operation reuse, retaining separate explicit sign-in and non-submitting rerun. Then run broader relevant regressions, update canonical evidence once, safely publish/integrate, release only this session's claims and lock, and verify remote readback.
