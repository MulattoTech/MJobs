# MJobs checkpoint — boundaries, scheduling and verified test matrix, 2026-11-02

## Latest coordinated sprint checkpoint — 2026-11-03

Run: webpro-20261103T034313Z-1e2ff1. Base main: 7e86511f409fb9d1e2dc79904b170cc74b88cd3d.
Integration preparation owner: 07cb9a0af294c16424a24a01719b0cdf6db2d8b6; requery before resuming.
Candidate branch: work/webpro-20261103T034313Z-1e2ff1-cohort.
Frozen source PRs: **894,895,897,898,900,901**. Exact heads/ancestry/source-blob proof:
[cohort record](../docs/evidence/2026-11-03-webpro-1e2ff1-cohort/source-assembly.json).
Source commits, failure histories and milestone evidence remain preserved.

**Only295 adds a proposed PASS**, for the fourteen-section reporting deliverable.
004/008/160/171/224/264 stay PARTIAL.171 moves NOT_RUN to PARTIAL; no PII umbrella acceptance.
899 is excluded pending owner corrections;269 bookkeeping stays with that owner.
Original366 and reference59 criteria/denominators remain unchanged.

Frozen source113081bc is now verified: Windows/PostgreSQL417PASS plus326 passing
subtests,8SKIP; Linux108PASS/0SKIP; scanner-policy6PASS. The six POSIX Windows
skips all pass on Linux; two live-source probes remain disabled. Report verification
passed14 required sections,60 local links and all2874 immutable manifest entries.
[Executed source receipt](../docs/evidence/2026-11-03-webpro-1e2ff1-cohort/source-execution.json).
Actual proposed-merge execution, fresh protected gates, protected merge/current-main
and guarded rollout remain pending. Tests bind113081bc, not this evidence successor.
Intended rollout owner: run89c21f, after a separate deployment ref/native lease.
Each source owner retains and releases their own objective refs; no branch rewrite.
[Independent source/canonical review and owner89c21f cleanup checkpoint](https://github.com/MulattoTech/MJobs/pull/894#issuecomment-6595985003).
That checkpoint preserves004/160/264 claims, frozen source worktrees and the exact
owner89c21f disposable PostgreSQL resource; only its owner may clean it up.

The containing integration PR final receipt will name actual candidate/merge/main,
deployed SHA/image/readiness, skips, resource cleanup and exact claim releases.
Owned worktree ends -1e2ff1-cohort; raw receipts/resources are in the run's
private -1e2ff1-evidence/cohort directory. PostgreSQL is owned loopback/tmpfs.
Do not run synthetic cases against production.

## Earlier checkpoints — historical observations


## Exact source and publication state

[PR890](https://github.com/MulattoTech/MJobs/pull/890) is protected-merged and guarded-deployed at **`e67c5dfcb14bfb126a93170819e2f5bd875665ec`**. Tested proposed `c7e3e7c0142c719110ab488bb03562d207451f4e` and actual merge share tree `c2eb3694105ad1d86b176ba9d6de319ac42d9b88`. Its [delivery evidence](../docs/evidence/2026-11-02-pro-boundaries/deployment.json) records image, seven services, backup, rollback image, actual runtime ACL/ORM probe and public readback.

[PR891](https://github.com/MulattoTech/MJobs/pull/891) is protected-merged at **`821171122a44332853339a798d717c5c11dd9ad5`**. All five protected checks and CodeQL passed on final head `a1f261acc45f1e0f066c31d2f717bdd8b5417e83`; exact proposed `54a31d0735ca04ee6bea6adae5a4891b7e9b5608` and actual merge share tree `887d197621783b6cd155d78afd9cb928675e50aa`. [Protected receipt](../docs/evidence/2026-11-02-pro-test-matrix/pr891-protected-merge.json) retains initial failures, corrected validation and successful final gates. Independent main gates and guarded deployment are separate; its final PR description records delivery.

Current branch **`work/pro-test-matrix-20261102`** adds a joined last-search deletion regression and publishes **MJ-REQ-291 Test Matrix** acceptance. **Read the final description of the PR containing this file before resuming.** It records its own future final head, proposed/actual merge, protected/main gates, deployed SHA/image/backup, issue readback and cleanup. A commit cannot contain its own future merge hash. Requery current GitHub main and `/__deployment`; this checkpoint does not claim the current publication is already merged or deployed.

## Acceptance and accounting

Sprint baseline main: **`0112ac4d5f62214925f43c2e5b68e01ad69e4322`**. P0 **12 PASS /4 PARTIAL /10 NOT_RUN /26**; P1 **106 PASS /7 PARTIAL /3 NOT_RUN /116**. Newly verified: **015 Domain Model — Core Concepts**, **174 Security — Database**, **291 Test Matrix**. An audit corrected an overstated historical **264 PASS to PARTIAL**, retaining all older evidence.

This publication records P0 **13 PASS /4 PARTIAL /9 NOT_RUN /26**, P1 **107 PASS /7 PARTIAL /2 NOT_RUN /116**, original **181 PASS /81 PARTIAL /104 NOT_RUN /366**. Separate reference authority remains **19 PASS /4 PARTIAL /36 NOT_RUN /59**. This is one new P0 and two new P1 minus one older P1 correction. Criteria and denominators are unchanged.

Initial GitHub inventory: **16 open P0 /18 open P1**. Nine already accepted requirements still had open issues: #12,#13,#17,#213,#219,#251,#252,#266,#269. Their [historical review](../docs/evidence/2026-11-02-pro-boundaries/historical-issue-reconciliation.json) supports status-only closure with zero new acceptance credit. New015/174 issues: #16/#175; new291: #292. Keep264/#265 OPEN. Final containing-PR receipt records actual closures; after all twelve, expected inventory is13 openP0/9 openP1. Do not claim unperformed closures.

## Engineering and verification

- **004 PARTIAL:** bounded PostgreSQL/SQLite cancellation-control queries; database-independent socket clock; safe socket ownership; unavailable authority propagates BLOCKED. Acquisition/pool checkout/connect/DNS/network black holes, arbitrary callbacks and recovery/finalization bounds remain unproved.
- **015 PASS:** immutable SavedSearchRule shared by settings and execution prevents malformed persisted automation from silently creating outbox/audit writes. All nineteen domain concepts remain distinct.
- **174 PASS:** actual ORM profile/version/fact reads preserve caller transactions, snapshots and return contracts. Parameterization and effective runtime least privilege verified. Production has no profile rows; populated/versioned/reopened evidence is isolated PostgreSQL. Live probe covers actual missing-row mapping and runtime ACLs only.
- **018 PARTIAL:** typed strategy/capability admission, current durable policy checks under import lock, preserved newer backoff, detached errors and corroborated job-feed fallback. Full preference ladder/provider breadth remains incomplete.
- **264 PARTIAL:** queued NORMAL-to-HIGH promotion preserves identity, authority, age, retries/backoff and cancellation; stale normal RQ delivery defers before an attempt. Both PostgreSQL lock orders pass. Automatic discovery without a verified employer origin remains missing. Running work and COMPANY_SOURCE_REFRESH are unchanged.
- **291 PASS for the literal matrix:** original72 scenarios/12 groups map to65 primary selectors, all executed PASS with no selected skips.67 serialized parent/parameterized rows contain successful unittest subtests folded into parents. Independent review corrected remote-filter, actual dedupe and production-XSS mappings. See [execution](../docs/evidence/2026-11-02-pro-test-matrix/source-execution.json), [review](../docs/evidence/2026-11-02-pro-test-matrix/review.json) and [acceptance](../docs/evidence/2026-11-02-pro-test-matrix/acceptance.json). Broader171/172/224/292 remain incomplete.

New regression uses real supported PERSONAL APIs and a worker with only synthetic provider transport. Empty search import twice, reopening, six populated tables, both profiles and the untouched profile's completed history are checked; stale queued collection cannot call the source collector. Existing DEAD_LETTERED/NoResultFound behavior is preserved. No production behavior change or same-profile prior-history claim. Final Windows adjacent suite: **17 PASS /19 subtests /0 SKIP**; overlapping local runs are not summed.

PR890 exact native scope: **285 PASS /2 platform SKIP /275 subtests**; both skipped cases passed hosted Linux. PR891 final proposed scope: **144 PASS /3 symlink SKIP /44 subtests**; counterpart cases passed Linux. Additional nine Chromium cases passed, zero skips. Final hosted unit:7275 JUnit cases including subtests/53 skips; integration:4153 including subtests/39 skips,zero failures/errors. Both native Redis-skipped priority cases explicitly passed hosted Linux/Redis. No full-report skip is relabeled PASS.

Sourcee67 fresh FULL readiness passed13 checks, including before/after live readiness/exact clean source. Confirmed application is confined to the owned mock, repeated confirmation remainsone, cleanup passes. Brave readback: Services ready, External submission OFF, exact SHA. Unauthenticated CLI returned403; no login-flow claim. Current publication requires its own delivery verification.

Preserved failures: deadline/socket regressions; PR891 stale hard-coded census assumption corrected with controlled canonical/Project states; reused fixed-identity native database fixture retained before a fresh DB passed all10 retry tests; new deletion fixture's disabled source corrected by supported PUT; native npm PowerShell wrapper stopped on compatibility-warning stderr, then captured install/build exits passed. Read dated evidence rather than erasing failures/skips.

WWR remains SOURCE_DUPLICATE_JOB_ID:100 entries/98 identities, two equal complete GUID/link pairs with conflicting description/type. Both input orders refused, stored rows unchanged. Exact repeats already work; no unsupported winner or repeated public polling without new evidence.

## Workstation and next batch

Owned native worktrees under `C:\Tools\Dev\`: `MJobs-pro-bounds-domain-20261102` (source890), `MJobs-pro-boundary-merge-20261102` (verification/raw receipts), `MJobs-pro-priority-acceptance-20261102` (source891), `MJobs-pro-test-matrix-20261102` (current publication), clean unused `MJobs-pro-source-contracts-20261102`. Final containing PR records cleanliness/unfinished work. Only fast-forward primary after checking cleanliness. Preserve13 unrelated dirty files in `MJobs-fleet-recovery-20261026` and locked `.ao` trees.

Owned disposable PostgreSQL: `mjobs-pro-boundary-pg-20261102-01`, exact ID `bdf1629274d0ef8b769a163992a5802ec85c395b7ac482cc5e47e455b2024322`, label `mjobs.sprint=20261102-bounds`, loopback42692/tmpfs. Remove only this exact identified container after validation; final PR records cleanup. No live restore, host reboot or unrelated process termination.

Next: **004** acquisition/connect/network/finalization deadlines; **264** safe automatic employer-origin discovery; **018/017** strategy/provider breadth. **008** host restart and **292** complete original journey remain unverified. **171/172/224/269** remain substantive gaps. Prior accepted162/167/175/208/267/275/288 and minimatch remediation predate this sprint. No paid AI, real submissions, fleets or standing worker claims are authorized by this handoff.

## Latest sprint checkpoint: webpro-20261103T031738Z-642e4b

UTC checkpoint: 2026-11-03T03:33:06.273418+00:00
. Source branch: work/webpro-20261103T031738Z-642e4b-172.
Source revision: d096d03deba8aa749f478f9dd6c91029379ec50f.
Objective: MJ-REQ-172 / issue173; scoped web-origin repair, PARTIAL only.
Exact committed-source scope: 151 tests and 33 subtests PASS, zero skips.
Frontend build/typecheck PASS; independent internal code review found no blocker.
Both earlier fixture failures and corrections are retained in
[execution evidence](../docs/evidence/2026-11-03-webpro-642e4b-web-security/source-execution.json).
No migration, credential change, real employer submission or paid model call.

Baseline main and observed deployment:63b843dfe2706e6a5ac1d567af7e5acb01e134a7.
Protected PR, actual merge, new main checks and deployment remain pending at this
file's creation. Read the containing PR's final receipt for those future hashes.
P0 remains 13/26 PASS. P1 remains 107/116 PASS; only 172 moves NOT_RUN to PARTIAL.
Do not close issue173 or claim universal CSRF, auth-policy or product acceptance.

Objective claim refs/heads/webpro-claims/MJ-REQ-172 is owned by
1c26033405156715b21679f241182bd221971b52. Integration preparation used
refs/heads/webpro-locks/integration at 59274c50197417458c7fb4a3418cfb2f7ac4c421.
Requery both refs before any resume; this record does not preserve ownership.
The preparation lock will be released while CI runs and reacquired for merge.
No deployment lock has been acquired. The containing PR records final releases.

Isolated worktree:C:\Tools\Dev\MJobs-webpro-20261103T031738Z-642e4b-172.
Raw synthetic test receipts are under its outputs directory; public evidence
removes workstation hostname metadata. Existing unrelated worktrees remain intact.
Next independent candidate: 224 adapter contract gaps; scope and claim must be
rediscovered before writes. Original 171/224/269 and overarching P0 outcomes remain
incomplete.

## Web Pro checkpoint: verified report and Workday correction

UTC: 2026-11-03T04:18:08.154659+00:00. Run: webpro-20261103T031738Z-642e4b.

PR 893 / MJ-REQ-172 merged at ea5fbf9f0c3c27ecb0d86b90168f276df0349c6b; its tree exactly equals the tested
candidate. 151 tests/33 subtests and all five required gates PASS. Parent172 stays
PARTIAL and issue173 remains open. Merge receipt:
[protected integration](../docs/evidence/2026-11-03-webpro-642e4b-web-security/protected-merge.json).

Workday source 1b8da8cf27c989a8a23e3733f6a9098fca5e00c3:55 tests/23 subtests PASS,
no skips; independent review found no blocker. It corrects unsupported fixture
capability and preserves fail-closed DOM/deadline boundaries. Parent 224 remains
PARTIAL; no real employer operation was performed.

[Implementation report](../docs/IMPLEMENTATION_REPORT.md) is complete at artifact
8ee14e8256596b39ffa3d0b25972856205680b0c, technical source1350f398386d0be8c9555aa8ec565daf186b2b84.
All 14 literal sections and all inventories were independently verified. The only
new PASS is MJ-REQ-295, the factual reporting deliverable. Broader product and
other delivery-control requirements remain incomplete. Global counts must be
refreshed on actual main after integration; do not attribute concurrent workers'
changes to this run.

Combined publication branch: work/webpro-20261103T031738Z-642e4b-295; worktree
C:\Tools\Dev\MJobs-webpro-20261103T031738Z-642e4b-295. Source-only224 branch is retained.
The containing PR's final receipt records its future candidate/checks/merge and
deployment results; these were pending when this checkpoint was authored.
No migrations, dependency upgrades, paid AI calls, public submissions, live
restores, or fleet launches were performed by this run.

Objective refs 172/224/295 remain owned respectively by
1c26033405156715b21679f241182bd221971b52,
0dc61e343fbd9f1ea2e121d9f6dd34734a2a757d,
9f043f65c866a2de109d3d2939b1ed75cc9d3631.
This scoped accounting preparation used integration owner c7cb05fff5beeb8a5cefa9bcaca95f34ad10b0a5.
Verify live ownership; recorded hashes are not permission to reuse/delete refs.
Preparation ownership is released after verification, before CI waits. Current
main at preparation: ea5fbf9f0c3c27ecb0d86b90168f276df0349c6b. Production observation remains the
prior 63b843dfe2706e6a5ac1d567af7e5acb01e134a7 baseline until a separate guarded
release verifies a new exact SHA. Other workers own 004/171/269; preserve their
work, evidence and refs and re-discover their current status before selecting
new work.

## Latest sprint checkpoint: webpro-20261103T034424Z-23e5

PR #896 is protected-merged at `7e86511f409fb9d1e2dc79904b170cc74b88cd3d`;
its tree equals the tested candidate `c2718534ac9dc76bb2dffdd5dbaf8db5be34bd20`.
All five required gates and CodeQL passed. Source/candidate tests: 48 PASS plus
46 passing subtests, UI 14/build PASS, no skips; independent internal review PASS.
This checkpoint records **269 PASS for the literal AI document fact-trace contract**.
Drafting is CLI-only, selected saved-profile keys are unsupported, and paid/live
inference is NOT_RUN. Deployment and issue #270 closure remain separate gates.

PR #899 adds the collector company-identity conflict repair: source `9a8d138f`
passed 141 tests plus 25 subtests; two opt-in live feed probes remain SKIP/NOT_RUN.
Parent 017 remains PARTIAL. A full-CI pagination fixture failure was independently
reproduced and corrected at `4eb58a1d` without changing the production gate or its
assertion; the module passed 30 tests plus 37 subtests. Final hosted rerun pending.
Read [269 acceptance](../docs/evidence/2026-11-03-webpro-23e5-ai-resume/acceptance.json),
[896 merge proof](../docs/evidence/2026-11-03-webpro-23e5-ai-resume/pr896-protected-merge.json),
and [source/CI evidence](../docs/evidence/2026-11-03-webpro-23e5-company-identity/).

Source worktrees: `C:\Tools\Dev\MJobs-webpro-20261103T034424Z-23e5-269` and
`C:\Tools\Dev\MJobs-webpro-20261103T034424Z-23e5-company-identity`.
Objective claims: MJ-REQ-269 (`a0948cd5`), AGGREGATOR-COMPANY-IDENTITY-COLLISION
(`f11768a3`), and CI-DEPLOYMENT-PAGINATION-CLOCK (`fc7a4b6d`). Requery exact live
refs before resuming. Integration preparation owner is `58e50228`; release after
publication, reacquire before protected merge. No deployment lock is held by this run.
The containing PR's final receipt must record its future actual merge/main gates,
guarded deployed SHA/image/backup/rollback, issue state and exact claim releases.
No production restore, employer submission, paid provider call or fleet mutation.

## 2026-11-03T05:47:47.657834+00:00 — run23e5 reconciliation follow-up

Protected cohort 906 is now main 9a21b98b29809a41771d963cd2d5679cf8c31f9a. The preceding dated exclusions and pending observations are retained as history. This owned preparation merges its complete clock regression and reviewed historical scanner fingerprint into 899, preserves all source-owner acceptance/evidence, and reconciles only 017/269. The redundant clock claim was released; no duplicate delivery credit is claimed.

269 literal functional PASS is recorded with the unchanged earlier acceptance receipt and the new [read-only production follow-up](../docs/evidence/2026-11-03-webpro-23e5-ai-resume/production-followup.json) at deployed 7e86511f. Production document history remains NOT_RUN because no profile/application exists. The final identity source 323f3e1c has 72 focused PASS; parent 017 stays PARTIAL. Its earlier failures/review correction remain retained. Current main 9a21b98b and 899 are not claimed deployed here.

Fresh combined-candidate execution and all five protected gates are still required before 899 merge. The integration owner is 95def0475e3527981e501607656d511b0409438c for this preparation only; it will be released during hosted verification. Source claims 269/a0948cd5 and identity/f11768a3 remain owned, revalidate exact refs before any continuation. The agreed 906 rollout remains with run 89c21f. No production write, provider call, restore or fleet launch was performed.

## 2026-11-03T05:59:31.437084+00:00 — run23e5 exact reconciled candidate verified

Candidate `11d5e7f48fde0c919faae02534c36edc4d1ff950` / tree `18239bc010e6035b6686788a3e53c6776ca22a27` passed 151 parent cases plus 83 subtests (234 reported), zero failures/errors/skips. All eight recorded commands passed, including owned npm ci/build. [Execution receipt](../docs/evidence/2026-11-03-webpro-23e5-company-identity/reconciled-candidate-execution.json) and [checkpoint](../docs/evidence/2026-11-03-webpro-23e5-company-identity/reconciled-candidate-checkpoint.json) preserve the earlier preflight refusal and warnings.

The initial CRLF/stat-only clean-tree guard refusal ran no tests; exact normalized text and JSON equality proved the three owned paths unchanged before their restoration. All prior source/checkpoint/failure history remains retained. The complete cohort clock fixture is preserved, with no duplicate claim or acceptance credit.

017 stays PARTIAL and 269 stays PASS under its existing scoped acceptance. Resolver-helper review is explicitly limited; independent final combined peer review and fresh hosted protected checks remain pending. Protected merge and the separately owned guarded rollout remain pending; this evidence successor is not claimed deployed. No push, merge, claim release, paid/live provider call or production write was performed by finalization.

npm 12.2.1 warned that installed Node 27.0.0-alpha is unsupported; owned npm ci and TypeScript/Vite build nevertheless passed. Hosted pinned-toolchain verification remains required. Vite retained its bundle-chunk warning for a chunk larger than 500 KB. No dependency or global runtime was changed.

## Web Pro checkpoint: protected cohort delivery and journal concurrency

UTC: 2026-11-03T06:06:15.840730+00:00. Run: webpro-20261103T034313Z-1e2ff1.

Protected PR906 merged as9a21b98b29809a41771d963cd2d5679cf8c31f9a,
tree05f41f9482f2b11ceffd63cf93fa100ef3bdb1af, equal to executed candidate62508d7c.
Source PRs894/895/897/898/900/901 are merged. All15 final PR checks passed.
Actual main counts were refreshed: original366=182PASS/83PARTIAL/101NOT_RUN;
P0=14/4/8 of26 and P1=107/9/0 of116. Only the295 reporting deliverable
adds full acceptance in that cohort, attributable to run642e4b; these are not
new acceptances claimed by the journal follow-up. Separate59 references unchanged.
The exact-main CI, PostgreSQL/Redis, secret scan and deployment-candidate gates
subsequently passed. Run89c21f owns deploymentb6c64e75; rollout verification
remains pending at this checkpoint. Read the final906 discussion for later receipts.

PR908 binds source6c9dc906 and evidence4272c0c1 to the reproduced successful-writer
lost-update defect. Record and sync now share a persistent local OS lock across
read, validation and both publications, with a three-second contention budget.
Native Windows36PASS+25subtests/1POSIXskip and Linux37PASS/no skips were executed
on committed source and initial proposed merge8c9cd974. The POSIX case passes on
Linux, and real process death releases the lock for fresh-process repair.
Independent review and final-head candidate/protected checks remain pending.
MJ-REQ-008 staysPARTIAL; all physical-host/power-loss, older/manual writer,
network filesystem and already-lost-history limits remain explicit.

Evidence: ../docs/evidence/2026-11-03-webpro-1e2ff1-journal-concurrency/source-execution.json;
candidate-execution.json; cohort-protected-merge.json; owned-resource-cleanup.json.
Owned disposable PostgreSQL containerbe29dbb9 and networkfd5541b4 were removed
after exact ID/name/label, sole endpoint and zero-other-client verification.
Owned worktrees, source branches and raw execution receipts are retained.

008 objective ref18af7a5b remains owned. This preparation uses integrationccad8a38
only for additive canonical reconciliation and will release it after publication,
before waiting on review/CI. Requery refs before resuming; this file grants no
ownership. No main advance or competing deployment is performed in this window.
Worktree:C:\Tools\Dev\MJobs-webpro-20261103T034313Z-1e2ff1-008-concurrency.
Safe next step: independently review908, execute its final current-base candidate,
require all protected gates, and serialize merge after the ongoing906 rollout.
Preserve899/902/907 work and907's distinct .gitignore additions on reconciliation.
