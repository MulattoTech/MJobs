# Comparison salary scope and provenance repair

Run: `mjobs-web-20261120-d9b89794` (ChatGPT Web).
Scope: [PR #1045](https://github.com/MulattoTech/MJobs/pull/1045),
[MJ-REF-039 / issue #415](https://github.com/MulattoTech/MJobs/issues/415).

## Revision and ownership

- Verified protected main: `f912491d17ad562b4ca094042626fa17341a632a`.
- Previous PR head: `92e56bc39c3d6e2693b4577056d2e9188f453403`.
- Exact source commit tested below: `6c5db928b316ee751453fe25d71f288b36d872a8`.
- Isolated continuation branch: `web/20261120-d9b89794/comparison-saved-filters`.
- Existing PR branch: `web/mjobs-priority-20261116-67704a68/mjref039-client-recovery`.
- Root remains the only integration/publishing owner. Two subagents edited disjoint
  backend source/test files; a third independently reviewed all six comparison files.
- The primary checkout and original PR checkout both contained handoff work.
  Their files and local branches were preserved. Current main was merged into an
  isolated continuation; the existing PR's authorship/history is retained.
- Live inventory found 69 open PRs, 67 development and two dependency PRs.
  Only #1045 overlaps any of the six comparison source/test paths.
  Publication is one ordinary update to #1045, with no new PR and no force push.

## Behavior

Comparison now forwards the saved salary period and availability policy into
the existing Store.jobs query. Hourly jobs remain eligible under hourly bounds;
ALLOW_UNKNOWN preserves missing-pay eligibility; KNOWN_ONLY rejects missing pay
even without bounds. Default annual/no-filter behavior and currency, period,
estimate, and numeric-range exclusions remain unchanged.

The pure comparison service now sorts optional provenance references safely in
both shared and job-specific requirements. Absent and populated references stay
distinct; no evidence is discarded or rewritten. This is a service-contract
correction, not a demonstrated production API crash: today's API projects one
source/reference pair per job.

The prior PR's typed scope/stale handling and safe Change job selection action
are preserved and validated together with these backend repairs.

## Failing-first reproduction

API tests were added before changing the API source:
**3 failed, 9 passed, 17 subtests passed**. Hourly and ALLOW_UNKNOWN comparisons
incorrectly returned 409; KNOWN_ONLY admitted missing pay with 200.
After the four-line API repair: **12 passed, 17 subtests passed**.

Two pure-service regressions were added before changing the sorting:
**2 failed, 7 deselected**, both with TypeError comparing NoneType and str.
After the repair: **9 passed**.

The new tests verify database read-only behavior, legacy defaults, exclusion
rules, preserved references, deduplication, deterministic reverse-input behavior,
selected-job order, and input immutability.

## Exact committed-source verification

On source commit `6c5db928b316ee751453fe25d71f288b36d872a8`:

```text
python -B -m pytest -q -p no:cacheprovider tests/unit/test_job_comparison.py tests/integration/test_job_comparison_api.py tests/integration/test_job_comparison_api_recovery.py tests/integration/test_job_comparison_app_mount.py tests/browser/test_job_comparison_workspace.py tests/browser/test_job_comparison_scope_recovery.py --tb=short --junitxml=<private-receipt>
```

**PASS: 30 tests and 28 subtests, zero failures/skips, 33.96 seconds.**
This includes two connected Chromium browser tests against the rebuilt application
using disposable synthetic SQLite fixtures and ephemeral ports.

```text
npm --prefix apps/web test -- --coverage.enabled=false src/job-comparison-client.test.ts src/job-comparison-workspace.test.tsx src/job-comparison-workspace-recovery.test.tsx
```

**PASS: 20 tests across three files**, 0.188 seconds on the committed source.

Also executed PASS: scoped Python Ruff, catalogue validation, progress journal
validation, dashboard freshness, source commit provenance (four newly authored
source/test files), and git diff --check. Frontend lint (313 files) and
typecheck/build passed on unchanged frontend blobs. Build emitted the existing large-bundle advisory; no build error.

Environment: native Windows, cached Python 3.12 environment with httpx 0.28.1,
SQLAlchemy 2.1.1, pytest 9.1.1; existing Chromium 1243. Imports were verified to
resolve inside the isolated worktree. Frontend dependencies were privately
copied from an unchanged-lockfile checkout; shared installations were not changed.
The initial API reproduction's source-identity imports caused a nonfatal AnyIO
pytest rewrite warning; the final committed-source command above did not use
that wrapper.

Independent review found no blocking findings in the six-file functional diff.
It inspected surrounding persistence/filter/client/UI contracts and verified
whitespace; test execution is attributed above to its actual author/root runs.
The initial optional Python lint run exposed four pre-existing diagnostics in
touched files. Root reviewed the import-order and supported UTC-alias cleanup
in commit 6c5db928, then reran the complete scoped suite on that exact revision.
This is AI technical review, not a human GitHub approval.

## Acceptance and release limits

No acceptance authority was edited. Original requirements remain **188 PASS,
82 PARTIAL, 96 NOT_RUN / 366**; additive reference gaps remain **19 PASS,
4 PARTIAL, 36 NOT_RUN / 59**. MJ-REF-039 remains NOT_RUN pending full-criterion
verification and authorized acceptance recording.

This evidence/checkpoint commit leaves tested source and test blobs unchanged.
Fresh hosted checks on its containing commit, protected merge, PostgreSQL/Redis
runtime validation, authenticated public smoke tests, and production deployment
are pending at publication. Consult live PR checks rather than historical status.

No real employer traffic, notifications, application submission, paid model call,
production data edit, service restart, tunnel/DNS/auth change, or acceptance
promotion was performed. Existing production and fixture stacks were preserved.
Rollback after any later authorized integration is a normal protected revert;
there is no schema migration or data rewrite in this comparison patch.

## Current-main refresh before publication

Main advanced to `db9cdc54f9b0fe060ffb58ad5afd9fa24ec59216` when the independent
ownership-index PR #1061 merged. This continuation merges that exact revision
without conflicts. All six reviewed comparison source/test blobs remain identical
to tested source `6c5db928b316ee751453fe25d71f288b36d872a8`.
The new ownership-index freshness check passes. Fresh hosted checks must validate
the final containing merge commit. Live publication preflight now counts 66 open
development PRs plus two dependency PRs; this update adds no PR.
