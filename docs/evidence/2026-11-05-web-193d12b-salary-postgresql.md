# MJ-TH-013 — PostgreSQL-safe salary filtering and sorting

Run: `web-20261105T000307Z-193d12b2020d`

Issue: #918 · Existing PR: #934

Claim: `refs/heads/webpro-claims/MJ-TH-013` at `01d75b33947f153c2766c360fa72888d05731c8c`

## Finding and baseline

The independent review in [PR #934 comment 6624591522](https://github.com/MulattoTech/MJobs/pull/934#issuecomment-6624591522) identified unguarded JSON-to-float casts inside salary predicates. PostgreSQL may evaluate boolean conditions in an order that attempts a cast before the JSON type check.

This run combined protected main `42ba63525c93e26bfe7168c4875a21fee4a5c6cd` with the original feature head `60205222bd38a8600ba800d7e5e2d048d297e785` in an isolated worktree. The original source and evidence are preserved as merge ancestry. Pre-repair product tree: `e5a0f24324524325e055b6483250ef4dc3ac4c57`.

New API regressions executed against SQLite and a disposable, loopback-only PostgreSQL 17 database. The PostgreSQL database used only synthetic fixtures, a unique schema per test and bounded connection/statement/lock timeouts. Production data and services were not used.

**RED:** `tests/integration/test_salary_query_safety.py` before the repair returned HTTP 500 for both YEAR and HOUR bounded searches. Pytest reported **2 failed subcases, 10 passed test functions, 14 passed subtests**, no skips, in 29.52 seconds. The original SQLite checks passed. Prior green CI did not cover this database-specific failure.

## Repair

- Numeric conversion now occurs only in the numeric JSON branch of a SQL `CASE` expression.
- Apply the same guard to salary filtering and salary sorting; preserve existing sorting semantics.
- Invalid endpoint types remain ineligible for salary bounds and never become undisclosed salary merely because conversion is guarded.
- Preserve source attributes without rewriting, estimating or converting compensation.
- Add the same behavioral contract for SQLite and PostgreSQL: hourly/annual bounds before LIMIT, explicit unknown opt-in, valid one-sided bands, malformed values sorted after comparable annual values, and unchanged unfiltered metadata.
- Malformed fixtures include nonnumeric strings, numeric-looking strings, booleans, arrays and objects in either endpoint; negative, inverted, foreign-currency, estimated, missing and explicit-null cases are also covered.

The PostgreSQL tests use the existing hosted integration variable `MJOBS_TEST_POSTGRES_URL`; the integration workflow discovers this file automatically. They explicitly skip when no disposable database is supplied. Current ORM/query behavior is exercised; migration acceptance is outside this change.

## Verification

Command:

```text
python -m pytest -q -p no:cacheprovider tests/integration/test_salary_query_safety.py tests/integration/test_job_sorting.py tests/integration/test_saved_search_filters.py --tb=short
```

- Focused repair verification: **48 passed, 51 passing subtests, no failures or skips**, in 251.94 seconds. The previously failing hourly/annual PostgreSQL requests now return HTTP 200 and the expected bounded results.
- `python -m tools.verification.catalogue`: **PASS**.
- `python -m tools.verification.progress validate`: **PASS**.
- `python -m tools.verification.progress_dashboard --check`: **CURRENT**.
- Working and staged `git diff --check`: **PASS** before publication.
- Exact source commit and final hosted checks are recorded in the PR continuation receipt; this file cannot contain its own future commit SHA.

## Boundaries and release

This run independently reproduced the earlier reviewer finding and authored the repair. Its self-review is not a new independent approval. Original feature UI/source history remains intact. Hosted current-candidate CI, any required review, serialized integration and production validation are separate gates.

No schema migration, dependency change, real employer submission, paid model call, autonomous worker launch, Fleet cutoff change or canonical acceptance change was performed. MJ-TH-013 remains PARTIAL pending delivery verification. Original P0/P1 counts and literal acceptance are unchanged.

Rollback uses the protected PR/revert workflow; the repair does not modify stored data.
