# Preview service error containment — MJ-REQ-172

- Run: web-20261105T000000Z-30d19610f4a3.
- Issue: #173; MJ-REQ-172 remains PARTIAL.
- Base: 42ba63525c93e26bfe7168c4875a21fee4a5c6cd.
- Claim: refs/heads/webpro-claims/MJ-REQ-172.
- Owner: 73a0a0ca5a215390b819facf71851b3d4f57e830.
- Ownership: HTTP 201 plus matching readback at 2026-11-05T00:01:53Z.

## Defect and scope

Two preview routers reflected raw service exception text. Synthetic fault
injection through actual HTTP routes supplied private-like diagnostic text to
extractor TypeError/ValueError, result-validator TypeError/ValueError and
prefilter ValueError paths. All five cases failed before the correction.
This demonstrates missing error containment, not a real user-data incident.

The routers now emit fixed short detail messages, preserving the existing
HTTP status, machine code and JSON shape. Successful results, scope checks,
request validation and storage behavior are unchanged. No retry, external
request, schema migration or model invocation is added.

## Reproduction

On the unchanged product source at the base above, with only added tests:

    python -m pytest -q -p no:cacheprovider tests/integration/test_browser_job_extraction_api.py tests/integration/test_cheap_prefilter_api_scope_security.py -k "private_service_errors or private_result_errors or private_prefilter_errors" --tb=short

Result: 5 failed, 2 passed, 6 deselected in 3.15 seconds (exit 1).
Four failures are subtest cases; one is a regular test case.

## Verification after correction

    python -m pytest -q -p no:cacheprovider tests/integration/test_browser_job_extraction_api.py tests/integration/test_cheap_prefilter_api_scope_security.py tests/unit/test_validation_error_privacy.py tests/unit/test_request_security_boundary.py --tb=short

Result: 133 passed, 7 subtests passed in 28.10 seconds (exit 0).
Tests prove bounded non-reflective errors, unchanged persisted tables and a
successful preview after each handled failure. Existing success and profile
isolation scenarios pass.

Tested source/test Git blob identities:

- apps/api/browser_job_extraction.py: d6cb52026c3cacbb7ebc30f599d9620b5a887cf9
- apps/api/cheap_prefilter.py: 271d30aca807cf26481c99ee82d869bd8d0cfe03
- tests/integration/test_browser_job_extraction_api.py: 1d8d8ae7964194af9cf91cf92b6eda1ef54bda5b
- tests/integration/test_cheap_prefilter_api_scope_security.py: b4f13a866a81c63255a4ccecf5f7a3a11ae12e65

Catalogue validation, progress journal validation, dashboard consistency and
git diff --check: PASS. These checks do not promote any acceptance result.

## Review and release boundaries

The author reviewed the small source diff; independent review is NOT RUN.
Protected hosted CI, actual merge candidate, integration and deployment are
pending. Full PostgreSQL/Redis, browser/mobile and production smoke are NOT RUN
locally. The focused fixtures use isolated synthetic SQLite stores.

No original P0/P1 acceptance row or denominator changes. Cloudflare policy,
production services, fleet controls and the 7 percent cutoff are untouched.
Rollback is a protected revert of the source commit; no data migration is needed.

## Hosted scanner finding retained

The initial hosted secret scan failed: run 41939033837, artifact 13375980064.
Its two generic-api-key findings are the Git blob IDs on lines 45 and 46 of
this file in source commit c5339366e7372f9a5fca24c4c0410edacf8a6b2d.
Both were verified against that immutable commit with git rev-parse and exactly
match the source blobs listed above. They are source identities, not credentials.

Under scoped SECRET-SCAN-FINGERPRINTS ownership, only those two immutable
commit/path/rule/line findings were added to the existing reviewed exception
list. No scanner binary, detector rule, workflow, directory or wildcard
exemption changed. Author triage is documented in PR937 comment 6645844602;
independent review and the next hosted scan remain separate gates.
