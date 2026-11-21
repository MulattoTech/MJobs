# PR933 current-main integration verification

Run: web-20261105T024512Z-8bff6fc6-0a03-44d0-b6f2-3c98498e9165.
Requirement: MJ-REQ-264 / issue #265 remains PARTIAL.

## Ownership and revision

The previous implementation/integration owners released their claims in the PR.
This run acquired refs/heads/webpro-claims/PR-933-INTEGRATION at
963eb802e42f55af8976ff40c0190f83ddba3c45 with HTTP201 and exact readback at
2026-11-05T02:50:16.9798705Z. The short integration lock owner
84225538294659f715f60169ec9d15d1b6943529 was separately created with HTTP201
and exact readback at 2026-11-05T02:53:08.0201233Z.

Published input: 34576088ee39d41b669e76280f1414afe65c869f.
Protected-main input: d6650e4bc9bfd784bd6e3a346c793c372f02518a.
Tested merge commit: 3da04afe4d21a7a4131948535e72fd39207945f7.
Tested tree: edd3bef15da0834907e6c0b5c9c464ea5bc78b84.

The merge preserves both original parents/history. Four reviewed discovery
source/test/evidence blobs are identical to the published input; the scanner
policy is identical to protected main. All 20 other open PR path inventories
were inspected: no discovery source/test overlap. Shared scanner deltas are
already byte-identical to main. No new scanner exception or product change was
introduced. The old scanner failure and original regression failures remain
preserved in the prior evidence.

## Executed exact-candidate checks

A fresh isolated Windows worktree used the existing pinned Python environment
read-only, with its own temporary directory/logs and current-worktree imports.
No dependency or shared environment was modified. Inherited MJobs service/test
configuration was removed only from the test child environment.

Command:
```text
python -B -m pytest -q -p no:cacheprovider tests/integration/test_company_discovery_policy.py tests/integration/test_company_discovery_idempotency.py tests/integration/test_company_discovery.py tests/integration/test_company_discovery_requests_migration.py --tb=short
```

Result: **59 passed, 24 skipped, 33 passing subtests; zero failures**, exit 0,
34.97 seconds pytest time. The 24 skips explicitly require disposable PostgreSQL.
They are NOT local PASS. Synthetic fixtures use isolated SQLite and mock HTTP;
no live applicant data, employer requests, application submission or model calls.

Catalogue, journal validation, dashboard freshness, whitespace and strict commit
provenance all passed on the tested merge. HEAD was asserted before and after.
Initial discovery found rg unavailable; native Git file enumeration was used.
This was a tool availability issue, not a product-test failure.

The existing independent technical review is preserved:
https://github.com/MulattoTech/MJobs/pull/933#pullrequestreview-5777831102
It is separate from this run's source inspection and candidate execution and
does not represent human approval. No unresolved review threads were returned.

## Remaining delivery gates

The evidence-only containing commit leaves tested source blobs unchanged.
Fresh protected hosted checks, latest-main/head/candidate verification, thread
resolution, protected merge and guarded deployment remain separate gates.
The global integration lock is released after the bounded publication transaction;
the PR conversation records its exact release and subsequent state.

No canonical acceptance, issue closure, denominator, Project/Fleet state,
production, quota or emergency-cutoff change is claimed. MJ-REQ-264 remains
PARTIAL because general automatic employer-origin discovery is still incomplete.
No heavy/browser resource slot was used. Rollback remains a protected source
revert; this integration adds no migration or live-data rewrite.

## Follow-up: executed disposable PostgreSQL checks

At 2026-11-05T03:10:19.920460+00:00, run 8bff6fc6 completed the previously skipped PostgreSQL cases at published head **84af225c3ca532c06ae5c15c34306f2ad97848eb**:

`python -B -m pytest -q -p no:cacheprovider tests/integration/test_company_discovery_policy.py::CompanyDiscoveryPolicyPostgresTests tests/integration/test_company_discovery_idempotency.py::CompanyDiscoveryIdempotencyPostgreSQLTests --tb=short`

**24 tests PASS, 22 passing subtests, zero failures/errors/skips**, pytest 12.07 seconds (subprocess 12.657 seconds). This exercises actual PostgreSQL job-lock deferral, both promotion/worker-claim orderings, renewal/replay/rollback policy and competing idempotent intents. The earlier SQLite run's 24 skips remain recorded above; this is a separate execution that supplies their PostgreSQL coverage.

The source checkout stayed clean and imports were asserted to resolve inside the owned worktree. PostgreSQL 17 used a unique disposable loopback database, no production mounts, tmpfs data, a 1 CPU / 512 MiB limit and a 240-second test-process bound. Cached PostgreSQL image ID: `sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f`. The harness removed its exact label-verified container and checked its absence. No Redis, browser, real employer, provider inference or production mutation was involved.

Resource admission used `refs/heads/webpro-slots/heavy-01`, owner `109b17c547208ff2cc7ff0eb4336bb4d119834d9`: HTTP 201 and matching readback, then owner-verified HTTP 204 and absent-ref readback at 2026-11-05T03:10:55.3363068Z. Receipts remain under the owned ignored output directory: `postgres-verification.json`, `postgres-focused.log`, `postgres-focused.xml`.

## Second protected-main refresh

Protected main advanced to **c6d499f391a8e092e06892d7fdea25cbfd177e53** (PR942). Integration ownership `c9c11af3b58e3ae29e84cf7589c16f8559cae4b7` was atomically acquired with HTTP 201 and exact readback at 2026-11-05T03:10:56.4651130Z. Both scoped work claims and published heads were revalidated before editing.

The clean non-destructive merge is **3c20c13e6fb32cc4a68d91537f48b21b3d248f5e**, tree **fbd9ed283daa2b3c790f96009fe959c1666dac5c**. Fourteen incoming paths concern extraction/ATS control handling and its evidence. Company-discovery requests, policy/idempotency tests, persistence models/Store, worker operations/dispatch and source collection blobs are verified unchanged from the executed PostgreSQL head. No reviewed source or test was rewritten.

The original independent review remains separately attributed. This integration/evidence update is not a new independent review, protected merge, deployment or full MJ-REQ-264 acceptance. Current-head hosted gates and the actual current-main merge candidate remain separate mandatory checks.

Refreshed merge candidate 3c20c13e completed the same four-file SQLite suite: **59 tests and 33 subtests PASS, 24 PostgreSQL-only SKIP**, pytest 41.28 seconds, exit 0. Those PostgreSQL paths are covered by the separate real-database execution above; they are not counted as passing in this SQLite invocation. Catalogue, journal validation, dashboard freshness, strict merge provenance and whitespace checks all passed. Refresh receipts: `outputs/web-8bff6fc6-refresh2/verification.json` and `focused.xml`. The final evidence-only commit preserves this tested source tree.
