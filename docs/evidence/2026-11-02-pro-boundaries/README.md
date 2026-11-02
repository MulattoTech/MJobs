# MJobs control boundaries and domain persistence — 2026-11-02

Baseline: `0112ac4d5f62214925f43c2e5b68e01ad69e4322`, original P0 **12/26**, P1 **106/116**. The initial source batch changed no acceptance result; the verified acceptance publication below preserves all original criteria.

## Implemented behavior

- MJ-REQ-004: durable control reads bound database lock waits and preserve stricter configured limits. An unavailable read stops work with the fixed `OPERATION_CONTROL_UNAVAILABLE` classification; it does not manufacture user cancellation. A separate monotonic clock can close request-owned sockets while cancellation observation is blocked. Callback execution cannot hold the socket-ownership lock. Shared in-memory SQLite transactions are rejected before any nested SQL can roll them back.
- MJ-REQ-174: the production profile read path now uses ORM entities mapped to the migration-owned profile, immutable version and fact Tables. Plain returned dictionaries retain the existing contract. Sessions preserve caller-owned transactions, savepoints and repeatable-read snapshots; Core writes, parameter binding and least-privileged credentials remain in force.
- MJ-REQ-015: saved-search automation is an immutable explicit domain rule shared by save/import/export and runtime validation. Unknown persisted fields fail closed, without falling back to source polling or writing an operation/audit receipt. Existing domain identities remain distinct.

## Verification and limits

[Implementation record](implementation.json) retains initial failures, tested file hashes and review findings. The containing protected PR supplies exact revision-bound tests and hosted results. Native PostgreSQL execution is separate from Linux tests that lacked PostgreSQL. The initial browser test lacked local Chromium; that environment limitation is not PASS.

The 100 ms SQL lock/query setting is not a certificate for pool acquisition, DNS/connect, a database network black hole or arbitrary callbacks. Request cleanup still waits for its observer; no daemon is abandoned to manufacture success. MJ-REQ-004 remains **PARTIAL**. Real employer submission and paid inference remain OFF.

The earlier handoff's RQ-maintenance TODO is stale for a scoped subcriterion: prior hosted evidence in `docs/evidence/2026-11-01-pro-p1/hosted-execution.json` already records natural workhorse maintenance/redelivery and uncertain-submission recovery. Rechecking it is not a newly implemented capability; host restart and complete original interruption acceptance remain outstanding.

## Review follow-up before integration

[Follow-up evidence](review-followup.json) records the source-admission and employer-feed batch incorporated into PR890 before merge. Independent review caught newer-backoff overwrite, masked durable control failures, capability callback exception leakage and generic news-feed promotion. The resulting fixes preserve source authority and failure history. Native and exact proposed-merge receipts supersede neither the initial failed runs nor platform-specific skips. MJ-REQ-018 remains PARTIAL alongside004; 015/174 awaited protected delivery at that historical follow-up; the later verified record below supersedes that pending state.

## Verified source delivery and acceptance publication

[PR890](https://github.com/MulattoTech/MJobs/pull/890) merged and deployed **`e67c5dfcb14bfb126a93170819e2f5bd875665ec`** with matching tested/merged trees. [Protected merge receipt](pr890-protected-merge.json), [deployment](deployment.json), and [fresh full readiness](full-readiness.json) keep source tests, production reads and isolated fixture work distinct. All seven application services use image `sha256:ec17fe04f1517daae18bb97b2c11998b2b11da8f458786afb53bf8f6a0fd7c05`; backup verification and retained rollback image are recorded. Production has no profile rows; no personal data was seeded.

[Literal criteria](criteria.json) and [acceptance record](acceptance.json) support new **015** and **174** PASS. Historical **264** is corrected to PARTIAL: safe in-place priority promotion and claim ordering are implemented/tested in this publication, while automatic discovery without established employer-origin proof remains incomplete. P0 stays12/26; P1 moves106→107 after two genuine acceptances minus one correction. Original180/366; reference19/59 unchanged.

The [priority receipt](priority-promotion.json) retains both pre-fix failures, native PostgreSQL races and separate skipped environments. The [historical issue review](historical-issue-reconciliation.json) reconciles nine stale statuses with zero acceptance credit. The containing publication PR final description supplies its own eventual exact merge, all required gates, final deployment and issue readback; those future actions are not self-attested here.
