# ATS hostname correction: protected integration continuation

- Run: web-20261104T235920Z-58c1462f-0bb0-4832-8e5b-483e6eb9d1f1; UTC: 2026-11-05T01:30:29.707740+00:00.
- Requirement: MJ-REQ-063 / #64. The canonical matrix already records PASS; no new acceptance credit.
- Existing PR: https://github.com/MulattoTech/MJobs/pull/935 . Source authored by web-9f4db49f; this run independently reviewed and integrates it.
- Previous head: 88f8e50299066b3f961c6fbe06fab37680afd65b. Protected base: 2e1c0072c2ea7241b832dc8842ce1cdc0d781cc1, after merged PR946. Tested merge-index tree before evidence additions: 169f9c2423e60453a14e0a3793b606e42134e66c.
- ATS detector and regression-test Git blobs remain identical to the reviewed previous head. The only runtime changes added by this refresh come through protected main.

## Preserved history

Independent ATS review and focused proof: 36 tests / 82 subtests PASS, https://github.com/MulattoTech/MJobs/pull/935#issuecomment-6645706011 .
The previous 88f8e502 CI failed on the old DOM phase test and emitted an unawaited-capture warning: https://github.com/MulattoTech/MJobs/pull/935#issuecomment-6645856771 . That failure is retained. The scoped remediation reached protected main through PR946 after all five exact-head checks passed; this refresh is not an unchanged retry.

## Current combined proof

Executed from the isolated review checkout with the repository-pinned private interpreter, synthetic fixtures, MJOBS child environment cleared, and RuntimeWarning/unraisable warnings treated as errors.

    -m pytest -q -p no:cacheprovider --basetemp C:\Tools\Dev\MJobs-Web\logs-web-58c1462f-935\pytest-after946 tests/unit/test_ats_detection.py tests/unit/test_lever_source.py tests/integration/test_application_capability_filter.py tests/unit/test_browser_debug_capture.py tests/unit/test_fleet_t12_debug_capture_budget.py tests/unit/test_p0_evidence_secret_fingerprints.py tests/unit/test_secret_scan_sustain_176.py --tb=short -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning

**77 passed, 113 subtests passed in 8.75s**. Catalogue, canonical progress and dashboard checks passed. Staged diff and strict commit provenance are required before publication. The source and tests are unchanged between this test and the final evidence commit.

Publication uses the complete candidate-specific existing-PR gate, binding the previous/new head, exact source and base branches/repositories, publisher, owner and meaningful scope. Local and remote branch names are separately verified. Push is a normal ancestor-preserving update of the existing PR.

## Limits and next gate

Fresh hosted checks on the new published candidate are required before serialized protected merge. Current main deployment, live ATS/browser behavior and full-project acceptance are separate. No real external application, provider request, production data change or additional model is used. Revert of the narrow detector source is the rollback route if needed; no migration is included.
The existing updater's earlier cf882ef5 release was independently verified for source/image/readiness and backup integrity: https://github.com/MulattoTech/MJobs/pull/939#issuecomment-6647013740 . That release does not include PR946 or this ATS correction. Authenticated Brave access remains unavailable and is NOT RUN.

## Current-main refresh after protected PR937

- UTC: 2026-11-05T02:16:05.291400+00:00; previous candidate: 41e4d43d2c3ab02b735f18281f62af5843b355c5; new protected main: 0d3fbc5749fba8c682033a343a7ec9695e4dd743; tested merge-index tree before evidence additions: 96b756861a68f34b8e6957762f42ca2491ebb156.
- The previous 41e4d43 candidate passed all five protected GitHub checks at 01:55 UTC with zero unresolved review threads. Another session held the integration lock and merged PR937 before this candidate could merge. Those passes remain historical evidence; they do not authorize this new head.
- Historical gate and changed-base receipts: https://github.com/MulattoTech/MJobs/pull/935#issuecomment-6647525866 and https://github.com/MulattoTech/MJobs/pull/935#issuecomment-6647559498 .
- The source and regression-test blobs remain exactly those independently reviewed in the original ATS change. Both append-only SESSION_HANDOFF contributions are preserved if a conflict occurs.
- Executed combined proof on the new main: **77 passed, 113 subtests passed in 8.89s**; RuntimeWarning and pytest unraisable warnings are errors. Catalogue/progress/dashboard checks pass. No assertion, scanner policy or acceptance status was weakened.
- Publication is a normal update of the existing PR after the complete candidate-specific preflight. Fresh exact-head hosted checks, current-main verification and a newly owned integration lock are required before merge. No global lock is held while waiting for CI.
- Production checkpoint: the existing scheduled updater deployed PR946 merge 2e1c0072; source/image/readiness and a fresh verified backup were independently checked. https://github.com/MulattoTech/MJobs/pull/946#issuecomment-6647559218 . This is distinct from this still-open ATS change. Authenticated Brave verification remains NOT RUN.
