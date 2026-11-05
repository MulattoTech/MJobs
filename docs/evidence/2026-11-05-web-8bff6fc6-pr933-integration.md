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
