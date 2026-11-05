# Closed-job pipeline readiness: executed regression and source verification

## Identity and scope

- Run: `web-20261105T000449Z-de48d939e269`.
- Requirement: `MJ-REQ-155`, issue [#156](https://github.com/MulattoTech/MJobs/issues/156).
- Stable defect claim: `refs/heads/webpro-claims/PIPELINE-CLOSED-JOB-READINESS`.
- Owner commit: `2f152b9e3ff136f23e8f225dd4b4ca1a794ac95d`; HTTP 201 Created and exact read-back at 2026-11-05T00:08:11.2669574Z.
- Base main: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`.
- Tested source commit: `a14c8ec78bfa35a02665f0b4293f0fe641ede0bb`.
- Isolated branch: `web/web-20261105T000449Z-de48d939e269/closed-job-readiness`.
- Other MJ-REQ-155 work owns `services/discovery/pipeline.py` cancellation/rollback. This change touches only the operations readiness projection, its UI blocker contract, and four focused test files. Shared API roots, discovery pipeline, canonical acceptance, schemas and deployment files are unchanged.

## Defect and behavior

The existing readiness projection ignored the canonical job lifecycle. A current score, approved facts and usable route could therefore advertise a CLOSED/FILLED/EXPIRED/REMOVED posting as ready and admit a fresh preparation request.

The projection now uses the canonical lifecycle status parser and closed-state set, returns a fixed privacy-safe `JOB_CLOSED` reason, and withholds preparation eligibility. The existing preparation API rejects a fresh closed snapshot with `409 PIPELINE_PREPARE_NOT_READY`. A job closed after the user's read retains the existing fingerprint protection and returns `409 PIPELINE_READINESS_STALE`. Neither rejection creates an operation or application.

Existing prepared documents and explicitly reported tracking remain visible and accessible. A persisted reopening restores normal readiness. Missing/unknown status and unstructured prose do not fabricate closure. No application submission, live provider, paid model, migration, or production data action occurred.

## Reproduction retained

On unchanged base product code, the new Python regression selection returned:
`8 failed, 4 passed, 26 deselected, 6 subtests passed in 50.57s`.

The failures comprised six lifecycle status subcases, a closed job with current prepared documents, and an API request accepted as `202 QUEUED` rather than rejected. The stale-fingerprint test already passed, preserving rather than inventing that protection.

The first attempt could not collect tests because globally installed Pydantic 2.14.2 required pydantic-core 2.50.2 but core 2.50.3 was installed. This is a retained environment error, not a product failure or successful test. A private per-worktree venv supplied the matching core; global packages were not modified.

## Executed source checks

| Command | Result |
| --- | --- |
| `python -m pytest -q -p no:cacheprovider tests/unit/test_pipeline_workbench.py tests/integration/test_pipeline_workbench.py --tb=short` | PASS: 32 tests and 19 subtests, 0 failures/skips; 115.27 s |
| `npm test --prefix apps/web -- src/pipeline-workbench.test.tsx --coverage.enabled=false` | PASS: 11 focused tests; this is not a coverage-gate claim |
| `npm run typecheck --prefix apps/web` | PASS |
| `npm run lint --prefix apps/web` | PASS: 296 files |
| `npm run build --prefix apps/web` | PASS: 139 transformed modules |
| `python -m pytest -q -p no:cacheprovider tests/browser/test_pipeline_workbench.py --tb=short` | PASS: 1 connected Chromium/API test; 10.07 s |
| `python -m ruff check apps/api/application_multipage_state.py services/operations/pipeline_workbench.py services/operations/pipeline_actions.py` | PASS |
| `python -m mypy --config-file pyproject.toml --cache-dir .tmp/de48d939e269/mypy-cache` | PASS: 3 source files |
| `python -m tools.verification.catalogue` | PASS |
| `python -m tools.verification.progress validate` | PASS |
| `python -m tools.verification.progress_dashboard --check` | CURRENT |
| `git diff --check` and `git diff --cached --check` | PASS |
| `python -m tools.verification.commit_provenance --commit HEAD` on source commit | PASS: 6 authored source/test files |

The browser test used a disposable SQLite database, owned ephemeral loopback server, new Chromium context and blocked external requests. It verified a closed job has no preparation button after initial load and reload, no preparation operation was created, reopening plus refresh restores the action, and one explicit preparation queues with profile isolation and truthful non-submission text.

The local Node 27 alpha/npm pairing emits an existing support warning; the bundle also retains its existing large-chunk advisory. Hosted pinned-Node checks remain separate.

## Resource and delivery state

Build/browser slots `webpro-slots/heavy-01` and `webpro-slots/browser-e2e-01` were acquired at owner `4be10e3e9fe065f7d625eab951a97804eae85a6a`, with 201 receipts and exact read-backs. Both were released after the browser result at 2026-11-05T00:16:06.1547859Z; DELETE returned 204 and prefix read-back returned zero refs.

Source is IMPLEMENTED and locally TESTED. Protected CI, independent review, actual merge candidate, protected merge and production deployment are NOT VERIFIED by this source note. Final receipts belong in the containing PR. MJ-REQ-155 remains PARTIAL; no original or additive requirement counts were changed.

Rollback is a protected revert of the source commit. There is no data migration. No integration/deployment lock was held for this implementation. The objective claim is retained until the durable PR handoff and exact-owner release receipt.
