# Lever optional salary safety — 2026-11-05

Run: `web-20261105T012951Z-f867bf6e69cc`.
Scope: `MJ-REQ-018` / issue #19, with the existing Lever employer pipeline under `MJ-REQ-155` / #156.
Base main: `2e1c0072c2ea7241b832dc8842ce1cdc0d781cc1`.
Atomic claim: `refs/heads/webpro-claims/LEVER-OPTIONAL-SALARY-SAFETY`, owner `6b14f97cc2fa745d9cf83170357f3c7ec51a071c`; HTTP201 plus matching readback at 2026-11-05T01:48:31.2920912Z.
Branch: `web/web-20261105T012951Z-f867bf6e69cc/lever-salary`.

## Problem and behavior

Lever's optional salary normalization called `math.isfinite()` before its existing numeric bounds. A valid JSON integer such as `10**400` raised OverflowError while converting to float. A JSON list/object in `salaryRange.interval` also reached a dictionary-key lookup and raised TypeError. Either failure aborted a valid complete employer inventory.

The normalizer now checks the existing numeric bounds without float conversion; these comparisons also reject NaN and both infinities. Non-string periods become UNKNOWN while valid amounts and the raw optional salary structure remain available. Invalid bounds retain the existing description-parsed fallback. The one-billion upper limit, bool rejection, ordered ranges, native IDs, regional URL validation, transport/deadline behavior and collection policy are unchanged.

## Reproduced baseline

Only the two regression test files were changed while the product module remained identical to the base-main blob.

```text
python -B -m pytest -q -p no:cacheprovider --basetemp .tmp/f867-lever/red tests/unit/test_lever_salary_safety.py tests/integration/test_lever_autodiscovery.py::LeverAutodiscoveryTests::test_optional_salary_faults_preserve_durable_inventory --tb=short
```

**Expected FAIL:** 10 failed results, 4 passed tests, 41 passing subtests; 10.93 seconds, exit1. The 10 includes eight failed unit subcases, the complete-inventory test, and the real collector regression returning SOURCE_COLLECTION_FAILED. Earlier failures are retained, not relabeled as passes.

## Focused correction result

The same command with isolated `green` basetemp returned **6 tests +49 subtests PASS**, zero failures/skips, 3.39 seconds, exit0.

The regressions cover oversized positive/negative bounds at both endpoints, booleans, strings, collections, missing values, NaN/infinity, negative/inverted/upper-limit ranges, finite boundaries/fractions, supported and unknown periods, and complete mocked public GET inventory. The connected reviewed-employer fixture confirms durable job/provenance/snapshot preservation, description fallback, explicit UNKNOWN period, healthy collection, database reopen and idempotent recollection alongside a valid neighboring posting.

No live Lever request, browser session, provider account, external application, paid model, shared dependency change or production mutation was used.

## Remaining gates and limits

At this initial source checkpoint, complete focused Lever regression execution on the committed source, repository validators, independent review and fresh protected CI remain pending. PostgreSQL-specific execution, live-provider behavior, browser journey and production are NOT_RUN locally. The new persistence fixture is SQLite; it does not certify an actual PostgreSQL migration or production dataset.

No canonical requirement result, original denominator, Project/Fleet completion or issue closure changed. Both umbrella requirements remain PARTIAL. Protected merge needs current-candidate checks and short exclusive integration ownership. Deployment is separately guarded. Rollback is a protected revert of this source/test change; there is no schema migration or data rewrite.


## Exact committed-source validation

The complete focused Lever regression set was executed at source commit `0001a608adc31292b70ece91408c76013228fc67` (tree `2784e210f034fae5581c63e19f2dc271aba98da6`):

```text
python -B -m pytest -q -p no:cacheprovider --basetemp .tmp/f867-lever/committed tests/unit/test_lever_salary_safety.py tests/unit/test_lever_source.py tests/integration/test_lever_autodiscovery.py --tb=short
```

**PASS: 29 tests and 56 subtests, zero failures/skips, 32.05 seconds, exit0.** This includes the complete existing unit and integration Lever suites plus the new malformed-optional-salary regressions.

Repository gates on that commit also passed:

- `python -m tools.verification.commit_provenance --commit HEAD`: complete provenance for three source files.
- `python -m tools.verification.catalogue`: PASS.
- `python -m tools.verification.progress validate`: PASS.
- `python -m tools.verification.progress_dashboard --check`: CURRENT.
- `git diff --check`: PASS.

The follow-up evidence-only commit preserves all three tested source/test blobs. Independent review, fresh protected CI, protected merge and guarded production validation remain pending at publication; this evidence does not promote either umbrella requirement.


## Test import-order follow-up

An explicit Ruff check of the new unit module at `60d1607868c87d9af862a92584f4241fb6f32fea` found one I001 import-order error. New atomic source ownership was obtained at `2026-11-05T02:08:45.4976873Z`: `refs/heads/webpro-claims/LEVER-OPTIONAL-SALARY-SAFETY` owner `2ca0d8b547edaf45d4564ebf0a47b21defcf682c`, HTTP201 and exact readback.

The follow-up only orders the standard-library imports and removes an extra blank line in `tests/unit/test_lever_salary_safety.py`; Lever production code and the connected persistence test are unchanged. The corrected focused command with `--basetemp .tmp/f867-lever/lint` reports **6 tests and 49 subtests PASS in 2.92s**, with no failures or skips. The transient remote process handle expired before its final exit receipt was collected; this test result was recovered from the complete persisted log, without rerunning the test. Ruff was independently read back as **All checks passed**. The prior full 29-test/56-subtest result remains applicable to unchanged production and test logic.

`python -B -m tools.verification.agent_skills` also passed; it validates repository metadata and launches no agents. A direct Python `-c` AST-comparison helper initially failed from PowerShell argument quoting, before analysis; executing the same helper from an owned file confirmed that the test AST excluding imports is identical. `git diff --check` passed.

Independent technical review of the previous frozen head is recorded in [review 5778806236](https://github.com/MulattoTech/MJobs/pull/954#pullrequestreview-5778806236), including 6 tests and 49 subtests independently passed. Applicability to this import-only follow-up, fresh hosted checks, current-base integration and deployment remain separate gates.
