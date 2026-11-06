# Combined job-workflow integration

- Run: web-20261104T235920Z-58c1462f-0bb0-4832-8e5b-483e6eb9d1f1; UTC: 2026-11-05T02:49:57.726915+00:00; primary existing PR934; protected main: d6650e4bc9bfd784bd6e3a346c793c372f02518a.
- Frozen components: PR934 salary search/range repair at dda83d43dc2b593749a48aa89adf8925aefc521b; PR935 ATS hostname correction at c583e54adee7c0f0c8271a18cd02f64c80e90cf0; PR951 Greenhouse/Remote OK corrections at c02ea3807373b45ff0df368c3a824f1a3be04a81. PR947 head e7996c4d65fbc9051bdfffdf1b940a516e037d75 is included through PR951.
- All seventeen authored source/test blobs are identical to their frozen published components after the current-main merge. No new runtime implementation or test relaxation is introduced by this consolidation. The adjacent JSON identifies every blob and its prior component.

## Why this candidate exists

Protected main advanced through PR953 after the separate candidates were published. This refresh carries the owned job-collection, ATS-classification and salary-search continuations through one protected integration candidate. All original author histories, independent review scopes and failed evidence remain preserved. Included PRs and branches stay unchanged/open until protected-main ancestry proves their delivery.

## Preparation check retained

An initial preparation guard expected sixteen authored source/test paths and stopped before any merge or test. Readback established the exact seventeen paths (nine salary, two ATS, six collector). The checkout remained clean and the integration owner was released with HTTP204/404. The corrected guard compares the entire explicitly reviewed path set; no source assertion or test is weakened. This was an orchestration inventory error, not a product-test failure.

A subsequent checkpoint merge stopped on Windows implicit cp1252 decoding before any product test. The first intermediate checkpoint was proven to be exactly the original byte stream mis-decoded and re-encoded. Recovery reconstructs the common checkpoint plus every distinct original component append from raw Git bytes, verifies the staged bytes exactly, and switches merge handling to raw bytes/explicit UTF-8. The failed receipts and intermediate history remain retained; no runtime source changed.

## Executed combined proof

    python -u -m pytest -q -p no:cacheprovider --basetemp C:\Tools\Dev\MJobs-Web\logs-web-58c1462f-934-range\pytest-combined-jobs tests/integration/test_salary_query_safety.py tests/integration/test_job_sorting.py tests/integration/test_saved_search_filters.py tests/unit/test_ats_detection.py tests/unit/test_lever_source.py tests/integration/test_application_capability_filter.py tests/unit/test_browser_debug_capture.py tests/unit/test_fleet_t12_debug_capture_budget.py tests/unit/test_p0_evidence_secret_fingerprints.py tests/unit/test_secret_scan_sustain_176.py tests/unit/test_greenhouse_salary.py tests/integration/test_greenhouse.py tests/unit/test_remoteok_salary.py tests/integration/test_remoteok.py --tb=short -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning

**182 passed, 1 skipped, 243 subtests passed in 154.62s (0:02:34)**. The sole skip is the existing opt-in Remote OK live-provider test; every new regression executes. RuntimeWarning and unraisable warnings are errors. The fourteen modules cover salary filtering/sorting/saved searches on PostgreSQL17 and SQLite, ATS classification and capability filtering, both collector persistence paths, diagnostic capture lifecycle and scanner policy.

Tests use the private pinned environment with inherited MJOBS/PYTHONPATH cleared, live providers disabled and only an owned loopback PostgreSQL fixture URL supplied. The fixture is capped at 512 MiB and one CPU. Its named data volume is labeled with this run, and both container and volume are owner-checked and removed after testing; the heavy slot is released immediately afterward. No live database, applicant data, external application or additional model is used.

Catalogue validation, canonical progress validation, dashboard consistency and staged whitespace checks pass. Strict commit provenance and full candidate-specific publication preflight are required before normal existing-PR push. Fresh exact-head hosted checks, current-main verification and a newly owned short integration lock remain required before protected merge. No global lock is held during CI.

## Preserved component evidence and review boundary

- Salary range/ordering: docs/evidence/2026-11-05-web-58c1462f-salary-numeric-range.md and adjacent JSON preserve the six original HTTP500 cases, original SQLSTATE22003, SQLite red test, intermediate ordering failures, pristine locale reproduction, corrected 50-test/55-subtest proof and all-seven-HTTP200 replay. This run authored that follow-up and cannot independently review itself. An independent reviewer is testing frozen dda83d43; its eventual result must be separately attributed rather than predicted here.
- ATS: docs/evidence/2026-11-05-web-58c1462f-ats-integration.md preserves the original spoofing regression, independently executed 36-test/82-subtest proof and prior exact-head gates. https://github.com/MulattoTech/MJobs/pull/935#issuecomment-6645706011 .
- Collectors: docs/evidence/2026-11-05-web-58c1462f-provider-salary-integration.md preserves both authors' red tests and independent review links; their prior combined candidate passed 61 tests/75 subtests with one explicit live-provider skip. https://github.com/MulattoTech/MJobs/pull/951#issuecomment-6647531391 and https://github.com/MulattoTech/MJobs/pull/947#issuecomment-6647227354 .
- Captures: the already-merged PR946 correction remains on protected main. This run independently observed deployed source/image/readiness/backup at main0d3f, before PR953 advanced main: https://github.com/MulattoTech/MJobs/pull/937#issuecomment-6648100367 . No deployment of this new candidate is claimed.

## Acceptance and next steps

Original366, reference59 and TalentHop acceptance remain distinct and unchanged. MJ-TH-013 remains PARTIAL; existing original statuses are not promoted by a combined green test. Authenticated Brave remains NOT RUN because its originating context is unavailable. Live providers, complete feature acceptance and production rollout remain separate.

After a successful protected merge, verify that the exact PR935, PR951 and PR947 heads are ancestors of resulting main before reconciling their PR state. Use only the existing guarded updater for verified current-main rollout, with fresh deployment ownership and backup/readiness proof. Rollback is a protected code revert; no schema/data migration is added here.
