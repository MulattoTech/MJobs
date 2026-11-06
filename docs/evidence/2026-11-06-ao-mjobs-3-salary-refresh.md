# MJ-TH-013: current-main continuation of PR934

Owner: AO session `mjobs-3`. Verified input PR934 head
`5020b7fa4e5afad84e3db8ef52d67e3b577fbead`; current main
`560b97eda0fcb0b80f4b63e8fe5b28e805414580`. Validation completed
2026-11-06 04:46 UTC on Windows, using the existing pinned Python 3.12
environment and local PostgreSQL 17.11. The containing commit identifies the
final candidate. No product implementation changed.

## Preservation and acceptance scope

- The conflict-free main refresh preserves all four frozen component heads
  (PR934/935/951/947). All 17 source/test blobs matched the prior refresh receipt
  before the browser test extension; the other 16 remain byte-identical.
- All 27 incoming main files other than the automatically combined canonical
  handoff match main exactly. The handoff merely combines existing histories;
  no new canonical handoff text or acceptance-matrix edits were authored.
- PR930 remains separate at `84d28a4d024a8f266fadd5e822736797a7231d5f`.
  Its API/UI changes were neither adopted nor modified.
- Existing tests and receipts cover units, compatible currency, bounds,
  malformed/estimated/unknown pay, raw provenance, defaults and saved profiles.
  The concrete remaining local coverage gap was real Back/Forward restoration
  and connected hourly-filter salary sorting. The owned browser test now checks
  these interactions, including clearing bounds when switching to annual pay.

## Executed checks

Commands run from the candidate worktree with its root on `PYTHONPATH`:

```text
python -X utf8 -m pytest -q -p no:cacheprovider tests/integration/test_salary_query_safety.py tests/integration/test_job_sorting.py tests/integration/test_saved_search_filters.py --tb=short -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning
```

PASS: **50 tests and 55 subtests**, zero skips, 230.59 seconds. An explicitly
disposable loopback `MJOBS_TEST_POSTGRES_URL` exercised PostgreSQL in addition
to SQLite. The session-labeled container used tmpfs data, one CPU and 512 MiB;
it was owner-checked and removed after validation. No production data was used.

```text
npm --prefix apps/web run test -- salary-search-controls.test.tsx
npm --prefix apps/web run build
python -X utf8 -m pytest -q -p no:cacheprovider tests/browser/test_saved_search_filters.py --tb=short -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning
git diff --check
git diff --cached --check
```

PASS: **5 Vitest tests**, coverage enabled; TypeScript and Vite build; **1
connected Chromium/API test** in 33.64 seconds; whitespace checks. The connected
test includes profile isolation, reload and saved-filter restoration, actual
Back/Forward navigation and an hourly-filter annual-only salary-sort request.
The unchanged build emits its existing large-chunk warning. Coverage targets
in the shared configuration are unrelated AI modules; this focused run does
not claim coverage of them.

The first web attempt stopped before tests because the initial cached
dependencies lacked `@vitest/coverage-v8`. Copying the complete existing PR934
validation cache resolved the prerequisite; no installation, global upgrade,
coverage suppression or product edit was needed.

## Separate remaining gates

Prior head `5020b7fa` had five required hosted checks successful at reconciliation;
those results do not certify this successor. Fresh exact-head protected checks,
native review, any required independent provider approval, serialized protected
merge and deployment remain separate pending gates. Authenticated live-provider
and production acceptance are NOT_RUN. MJ-TH-013 remains PARTIAL; no matrix,
issue acceptance, denominator or release status is promoted. No merge to main,
deployment, employer submission or host configuration change was performed.

Primary `mjobs-1` serializes publication and owns integration. Rollback remains
a protected source revert; no migration was introduced. Older component receipts
remain separately attributed and were not rerun without cause.
