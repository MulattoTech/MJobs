# MJ-TH-013: finite salary conversion and deterministic ID ties

## Result and attribution

This continuation fixes two independently reproduced gaps in existing PR934. It preserves the original web-5ad21717 salary feature and web-193d12b's guarded-type-cast repair. Run web-58c1462f is the author of this follow-up and does not claim independent review of its own new source.

- Baseline: c0f9d969a09d9b3a4f721b8cf0999b6fe42d90ca, the published PR934 head.
- Numeric guard source: 0400759d0fdc6ef2f2df48bb9abcd3e47949339e.
- Numeric guard plus salary ID-order correction: 2a443fbbea40ff68a14aa92cf46b6133ca2cd04b.
- Executed corrected batch: **50 tests and 55 subtests passed in 75.29 seconds**, no failures or skips, on PostgreSQL 17.11 / en_US.utf8 and SQLite.
- Original numeric-overflow HTTP probe replay: **all seven requests returned 200**, direct Store read raised no exception, stored source metadata unchanged.
- No canonical acceptance status changes. Full MJ-TH-013 acceptance, final hosted gates, browser verification, protected merge and deployment remain separate.

The source claim was acquired after the prior owner's explicit release, using unique no-op owner 5f2c69c1d286790b2db214239c162938633eb714, POST-ref HTTP201 and exact readback. Scope and baseline were announced at https://github.com/MulattoTech/MJobs/pull/934#issuecomment-6647297317 and https://github.com/MulattoTech/MJobs/pull/934#issuecomment-6647361752 .

## Reproduced failures, retained

### Valid numeric JSON outside floating-point range

Synthetic annual and hourly records contained an ordinary positive minimum and salary_max = 10**400. On the unmodified published source, a title-only read returned 200, but six requests returned 500: each unit's known-only query, each unit's combined min/max plus title ordering and LIMIT 1, and both salary-sort directions. The direct Store failure was DataError, SQLSTATE 22003. Original source metadata remained unchanged.

A new SQLite regression with positive and negative 10**400 endpoints and a finite sys.float_info.max boundary first produced **9 failed / 5 passed / 2 passing subtests**. This includes subcase failures; it is not nine distinct top-level test functions. SQLite admitted an overflowing number as infinity and could rank it ahead of a finite disclosed value. With the numeric guard, the SQLite-only safety class passed **6 tests / 10 subtests**.

### Existing locale-dependent salary tie order

The first combined batch on numeric-only source 0400759d produced **2 failed subcases / 50 passed / 53 passing subtests**, retained in the adjacent receipt. HTTP overflow failures were resolved, but both salary sort directions disagreed with the existing binary ID-order assertion in the unknown-salary tail.

A separate, pristine detached checkout of published c0f9d969 reproduced the same two ordering subcase failures on PostgreSQL 17.11 / en_US.utf8: **2 failed / 1 passed in 3.71 seconds**. This establishes that the ordering defect predates the numeric correction. The fixture locale was not changed to turn the test green, and the original assertions were retained.

## Implementation

The _finite_salary_amount helper keeps JSON type checking and numeric conversion inside a CASE expression. PostgreSQL's pg_input_is_valid(..., 'double precision') checks representability before the risky cast. SQLite values outside the finite floating-point range are excluded from comparable salary bands. Filtering and sorting use the same helper.

This is a representation boundary, not an arbitrary salary cap: the largest finite float remains known and sortable in the regression. Supplied malformed, overflowing, negative or inverted endpoints remain non-comparable according to existing semantics. Raw salary metadata is not rewritten.

Only PostgreSQL's salary-sort ID tie-breaker now uses explicit C collation, matching the existing binary-order expectations and SQLite behavior. Other title/company orderings and the database's configured locale are unchanged.

PostgreSQL 17 is already pinned in the repository's Compose and integration workflow. The input-validation function is documented at https://www.postgresql.org/docs/17/functions-info.html ; the actual PostgreSQL test and original probe confirm the conversion behavior used here.

## Commands and exact-source proof

From the owned isolated worktree, using the private repository-compatible Python environment:

    python -u -m pytest -q -p no:cacheprovider tests/integration/test_salary_query_safety.py tests/integration/test_job_sorting.py tests/integration/test_saved_search_filters.py --tb=short

The child environment clears inherited MJOBS configuration and PYTHONPATH, then sets only a synthetic loopback PostgreSQL fixture URL. The disposable PostgreSQL 17 container is restricted to 512 MB and one CPU. Ownership and heavy-slot admission are checked before use. No production database or applicant data is accessed.

The original HTTP probe uses the existing PostgreSQL safety fixture and FastAPI TestClient with exception propagation disabled. It inserts the two overflowing synthetic records and sends exactly the parameter sets preserved in the adjacent JSON receipt. On source 2a443fbbea40ff68a14aa92cf46b6133ca2cd04b every request is 200, the direct Store query succeeds, and assert_source_unchanged() passes.

Catalogue validation, canonical progress validation and dashboard consistency passed after these tests. Diff and strict commit provenance are checked before publication. New source commits carry truthful pre-test validation trailers rather than claiming tests that had not run when those commits were created.

## Cleanup, integration and limits

The owned disposable database was removed after the successful batch and original-probe replay; its heavy slot is absent on independent readback. The earlier failed fixtures and slots were also cleaned up. Only the objective claim remains for bounded integration and handoff.

Refresh current protected main under a newly acquired integration lock, preserve source/test blobs, validate the combined candidate, and run the full candidate-specific pr_queue --live --metadata ... --candidate ... --update-pr 934 preflight before a normal push to the existing PR branch. Release global locks before waiting for hosted CI. No new duplicate PR or force push is needed.

This follow-up has no schema migration, new dependency, production configuration change, external application submission or additional model use. The rollback route is a protected source revert. Previous failed results are preserved and no acceptance denominator or test assertion is reduced.
