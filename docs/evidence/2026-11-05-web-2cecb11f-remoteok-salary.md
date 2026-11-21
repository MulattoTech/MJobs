# Remote OK optional salary numeric safety

- Run: `web-20261105T000108Z-2cecb11f-ba3e-41b2-bf82-09d09e616213`.
- Scope: MJ-REQ-017.S07, issue [#308](https://github.com/MulattoTech/MJobs/issues/308); parent MJ-REQ-017 / [#18](https://github.com/MulattoTech/MJobs/issues/18).
- Starting main: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`.
- Source and regression commit: [0fc7f7b6](https://github.com/MulattoTech/MJobs/commit/0fc7f7b6139697cfaeaca0ecd8e7f565c3959e94).
- Exclusive objective: `refs/heads/webpro-claims/REMOTEOK-SALARY-NUMERIC-SAFETY`, owner `f64e3aa9676158280d581507e5377cd5df542d8d`; HTTP 201 and exact readback at 2026-11-05T00:19:52.2062519Z.
- Branch: `web/web-20261105T000108Z-2cecb11f/remoteok-salary`.

## Observed defect and resulting behavior

The adapter accepted a positive non-finite salary string through `float()` and then raised an uncaught `OverflowError` at `int()`. An excessively large integer could also raise during conversion. Boolean values became a fabricated positive compensation amount. These are optional fields; a malformed value must not discard an otherwise valid job.

The shared conversion helper now rejects booleans, failed or overflowing conversions, non-finite numbers, and non-positive amounts. A valid opposite bound survives. Finite numeric strings still work, and the existing salary-description fallback is preserved. The adapter does not mutate the raw input.

The mocked collection regression persists both affected jobs and their posting snapshots, retains the valid upper bound, records unknown salary where neither bound is valid, and leaves the source HEALTHY with two jobs seen. This is exercised through the existing collector and an isolated SQLite database, not only through the conversion helper.

Changed production scope is solely `services/discovery/sources/remoteok.py`. There are no transport, cancellation, source-capability, schema, migration, API or UI changes. Existing work on operation cancellation, shared discovery pipeline behavior and salary-filter UI remains outside this claim.

## Executed verification

All commands ran in this run's isolated worktree. No live Remote OK request, external application, paid model call or production mutation was made.

| Check | Result |
| --- | --- |
| Pre-edit normalization with `salary_min="Infinity"` | Expected FAIL: `OverflowError: cannot convert float infinity to integer` |
| New unit cases against the original implementation | Expected red: pytest reported **18 failed, 3 passed, 30 subtests passed** |
| Fixed adapter unit and existing collection integration files | **17 passed, 1 skipped, 47 subtests passed** in 8.33 seconds |
| Catalogue validation | PASS |
| Canonical progress validation | PASS |
| Progress dashboard check | CURRENT |
| Diff whitespace check | PASS |
| Strict source-commit provenance | PASS |

The red summary includes failing subtests; it is not a claim of eighteen separate top-level test functions. The single green-run skip is the existing opt-in live-provider test because `MJOBS_ENABLE_LIVE_TESTS` was not enabled. Every new regression case ran.

Exact focused command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .tmp/remoteok-salary tests/unit/test_remoteok_salary.py tests/integration/test_remoteok.py --tb=short
```

Other executed commands:

```powershell
.\.venv\Scripts\python.exe -m tools.verification.catalogue
.\.venv\Scripts\python.exe -m tools.verification.progress validate
.\.venv\Scripts\python.exe -m tools.verification.progress_dashboard --check
.\.venv\Scripts\python.exe -m tools.verification.commit_provenance --commit HEAD --strict
git diff --check
```

The private virtual environment uses the repository's existing `pydantic==2.13.5` and `pydantic_core==2.46.5` pins to resolve a pre-existing workstation package mismatch. No global dependency, repository pin or lockfile was changed. Local checks used no heavy-resource slot.

## Evidence limits, rollout and rollback

This note records local source verification at the commit above. Current-main integration, independent review of this new production patch, protected hosted CI, production rollout and live-provider verification are separate gates and are not implied by the local result. In particular, the SQLite collection test does not establish PostgreSQL acceptance for arbitrary malformed JSON or non-standard JSON numeric tokens.

Rollback is a normal protected revert of the narrow source commit after assessing intervening changes; no data migration is involved. This regression fix adds no canonical acceptance credit: MJ-REQ-017.S07's existing recorded status and the parent's literal acceptance criteria are unchanged. No original P0/P1, TalentHop, Project or Fleet status was edited.
