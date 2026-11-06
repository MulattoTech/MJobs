# Optional-salary collector integration: Greenhouse and Remote OK

- Run: web-20261104T235920Z-58c1462f-0bb0-4832-8e5b-483e6eb9d1f1; UTC: 2026-11-05T02:26:01.863442+00:00.
- Primary existing PR: https://github.com/MulattoTech/MJobs/pull/951 . Included existing PR: https://github.com/MulattoTech/MJobs/pull/947 . No new source PR is created.
- Greenhouse head: 3f3b546667894cd275387113fb80fc6b85ab3794; Remote OK head: e7996c4d65fbc9051bdfffdf1b940a516e037d75; combined history commit: 77a0404c65480ed74cc64cd91be16e2214090c82; protected main: 0d3fbc5749fba8c682033a343a7ec9695e4dd743; tested index tree before evidence additions: 72008a29845b3c15e3c8113cdbc80d0f5939f657.
- This run integrates prior authors' unchanged source; it does not claim authorship of the collector corrections or independent execution of their original red tests.

## Prior independent evidence

Greenhouse was independently reviewed and executed on Linux: 38 tests / 28 subtests passed, no skips, at the unchanged 3f3b5466 head. https://github.com/MulattoTech/MJobs/pull/951#issuecomment-6647531391 . Its original author's eleven pre-fix failures are retained in docs/evidence/2026-11-05-web-05329ef2-greenhouse-pay-range.md .

Remote OK was independently reviewed and executed on Linux: 17 tests / 47 subtests passed, with one opt-in live-provider skip, at unchanged e7996c4d. https://github.com/MulattoTech/MJobs/pull/947#issuecomment-6647227354 . Its original author's eighteen pre-fix failures, including subcases, remain in docs/evidence/2026-11-05-web-2cecb11f-remoteok-salary.md . Earlier integration/scanner evidence is also preserved.

## Combined-current-main verification

The six source/test blobs are identical to the two independently reviewed heads. They preserve valid jobs and raw provenance when optional salary fields are malformed; valid bounds and existing description fallbacks remain available. No shared transport, database schema or salary-filter source is edited by this integration.

Executed with private pinned Python, inherited MJOBS/PYTHONPATH removed, live-provider tests explicitly disabled, and isolated test outputs:

    -m pytest -q -p no:cacheprovider --basetemp C:\Tools\Dev\MJobs-Web\logs-web-58c1462f-951\pytest-integrated tests/unit/test_greenhouse_salary.py tests/integration/test_greenhouse.py tests/unit/test_remoteok_salary.py tests/integration/test_remoteok.py tests/unit/test_p0_evidence_secret_fingerprints.py tests/unit/test_secret_scan_sustain_176.py --tb=short

**61 passed, 1 skipped, 75 subtests passed in 28.89s**. The one skip is the pre-existing opt-in Remote OK live-provider test; every new regression runs. This batch includes both provider unit/collection suites and six scanner-policy tests. Catalogue, canonical progress/dashboard and staged diff pass; strict merge provenance and the complete candidate-specific publication preflight are mandatory before push.

No heavy/browser slot, Docker service, provider network request, production data or additional model is used. The integration lock is limited to this refresh/test/publication transaction and released before hosted CI.

## Attribution and protected next steps

The previous integrator explicitly released both scopes after contention. This run's fresh PR-951-INTEGRATION and PR-947-INTEGRATION claims use HTTP201 and exact readback; no stale claim was stolen. The existing PR951 branch receives a normal ancestor-preserving update. PR947 and its branch remain open/unchanged until protected main demonstrably contains its reviewed head.

Fresh exact published-head hosted checks, resolved reviews/threads, current-main verification and a newly owned integration lock are required before protected merge. Deployment is separate. Original MJ-REQ-018 and parent MJ-REQ-017 remain PARTIAL; no canonical counts, Project or Fleet statuses change. Live-provider, PostgreSQL-specific raw-payload behavior and authenticated browser verification remain unestablished by these local tests. Rollback uses a protected revert; no data restore is needed.
