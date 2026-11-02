# MJobs control boundaries and domain persistence — 2026-11-02

Baseline: `0112ac4d5f62214925f43c2e5b68e01ad69e4322`, original P0 **12/26**, P1 **106/116**. This source batch changes no acceptance result or original criterion.

## Implemented behavior

- MJ-REQ-004: durable control reads bound database lock waits and preserve stricter configured limits. An unavailable read stops work with the fixed `OPERATION_CONTROL_UNAVAILABLE` classification; it does not manufacture user cancellation. A separate monotonic clock can close request-owned sockets while cancellation observation is blocked. Callback execution cannot hold the socket-ownership lock. Shared in-memory SQLite transactions are rejected before any nested SQL can roll them back.
- MJ-REQ-174: the production profile read path now uses ORM entities mapped to the migration-owned profile, immutable version and fact Tables. Plain returned dictionaries retain the existing contract. Sessions preserve caller-owned transactions, savepoints and repeatable-read snapshots; Core writes, parameter binding and least-privileged credentials remain in force.
- MJ-REQ-015: saved-search automation is an immutable explicit domain rule shared by save/import/export and runtime validation. Unknown persisted fields fail closed, without falling back to source polling or writing an operation/audit receipt. Existing domain identities remain distinct.

## Verification and limits

[Implementation record](implementation.json) retains initial failures, tested file hashes and review findings. The containing protected PR supplies exact revision-bound tests and hosted results. Native PostgreSQL execution is separate from Linux tests that lacked PostgreSQL. The initial browser test lacked local Chromium; that environment limitation is not PASS.

The 100 ms SQL lock/query setting is not a certificate for pool acquisition, DNS/connect, a database network black hole or arbitrary callbacks. Request cleanup still waits for its observer; no daemon is abandoned to manufacture success. MJ-REQ-004 remains **PARTIAL**. Real employer submission and paid inference remain OFF.

The earlier handoff's RQ-maintenance TODO is stale for a scoped subcriterion: prior hosted evidence in `docs/evidence/2026-11-01-pro-p1/hosted-execution.json` already records natural workhorse maintenance/redelivery and uncertain-submission recovery. Rechecking it is not a newly implemented capability; host restart and complete original interruption acceptance remain outstanding.
