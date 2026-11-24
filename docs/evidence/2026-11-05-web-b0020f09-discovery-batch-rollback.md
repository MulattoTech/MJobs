# Discovery batch interruption rollback — 2026-11-05 UTC

- Run: `web-20261105T000420Z-b0020f09517e4be38ff4e7527be94d4a`.
- Requirements: MJ-REQ-155 / issue #156, with the bounded-operation constraint in MJ-REQ-004.
- Baseline: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`.
- Tested source: `ad683151ae86d955f2c5cf87610191f0ea4c55b0`.
- Claim: `webpro-claims/MJ-REQ-155` at `d8325d8ebd962d5b3179b86a31fd9de4b116c598`; acquired with GitHub HTTP 201 and exact readback.
- Source blob: `0196eecde91bcf017b1cb5d0653670a1d06b685a`.
- Regression blob: `15e4e82a67fb39f9d63653576942133e2a006a17`.

## Defect and correction

Fixture discovery checked operation authority before opening its job batch, but
did not check again while processing rows or before transaction exit. A cancellation
or expired deadline during that stage could therefore leave committed jobs,
postings, matches, history and a successful completion audit. In the public
API-to-worker reproduction, the operation was durably DEAD_LETTERED with
OPERATION_TIMEOUT while those batch results remained visible.

The correction checks between company/row steps and around the final completion
audit, inside the transaction. An observed control exception propagates unchanged
and rolls back the entire job batch. The owned HTTP client still closes in the
existing finally block. No schema, source identity, retry policy, API contract or
production configuration is changed.

Company/source enrollment is an earlier, separate durable step and is preserved.
The regression retries against those discovered sources and proves successful
completion with exactly two jobs, four postings and one completion audit.

## Executed checks

All data are synthetic. Servers use owned ephemeral loopback ports; each test has
its own SQLite database. The schema is initialized once and copied only after
closing its engine, avoiding redundant database setup without sharing mutable data.

| Check | Result |
| --- | --- |
| New direct cancellation/deadline tests against unchanged baseline | 9 FAIL; stop exception not raised |
| Public API-to-worker timeout against unchanged baseline | 1 FAIL; operation timed out but batch records remained |
| `python -m pytest -q -p no:cacheprovider tests/integration/test_mock_discovery_transaction_control.py tests/unit/test_mock_discovery_client.py --tb=short` | 12 PASS, 0 FAIL, 0 SKIP |
| `python -m pytest -q -p no:cacheprovider tests/integration/test_spine.py tests/integration/test_fixture_saved_search_discovery.py tests/integration/test_mock_company_environment.py --tb=short` | 10 PASS and 8 passing subtests, 0 FAIL, 0 SKIP |
| `python -m tools.verification.catalogue` | PASS |
| `python -m tools.verification.progress validate` | PASS |
| `python -m tools.verification.progress_dashboard --check` | CURRENT |
| `python -m tools.verification.commit_provenance --commit HEAD --strict` on source commit | PASS for both source files |
| `git diff --check` | PASS |

Coverage includes cancellation and deadlines, capture-only and scored discovery,
interruption after the first posting and after the completion audit, database
reopen, existing-history preservation, successful retry, exact exception identity,
client cleanup, and durable public-worker timeout status. Healthy discovery,
deduplication, saved-search filtering, and preparation workflows pass separately.

## Limits and release state

- This is cooperative checkpointing, not hard preemption of arbitrary callbacks,
  filesystem/database calls, or the final database commit.
- Direct cancellation tests inject the actual control exception via the pipeline
  callback; they do not claim concurrent public cancellation while a SQLite writer
  transaction is held. The public worker test verifies deadline propagation and
  durable rollback with a controlled monotonic clock.
- PostgreSQL-specific cancellation concurrency, Redis queue execution, browser
  E2E and production are NOT RUN in this local slice.
- Source review was by the author; independent review is NOT RUN here.
- Hosted required checks, protected integration and deployment are separate gates.
  This note does not assert them.
- MJ-REQ-155 and MJ-REQ-004 remain PARTIAL. No acceptance matrix, denominator,
  GitHub Project or Fleet acceptance state is promoted.
- Rollback is a protected revert of the scoped source commit; no data migration.

## Publication follow-up: historical scanner findings

At publication head a668590, hosted Security / secret scan failed in
[run 41940139374](https://github.com/MulattoTech/MJobs/actions/runs/41940139374).
Its redacted artifact contained exactly two findings in the older PR937 evidence
at revision c533936, lines 45 and 46. Independent Git-object inspection confirmed
both values are immutable integration-test blob identities, not credentials.

Under the separately acquired SECRET-SCAN-FINGERPRINTS claim, this follow-up
copies only the byte-identical two-entry .gitleaksignore delta already published
by PR937 at revision 23448e6. Each entry remains bound to one historical commit,
file, rule and line. No scanner rule, path wildcard or current-file exemption
changes. The original scan failure remains part of this history.

Executed scanner-policy regression command:

    python -m pytest -q -p no:cacheprovider tests/unit/test_p0_evidence_secret_fingerprints.py tests/unit/test_secret_scan_sustain_176.py --tb=short

Result: 6 PASS, 0 SKIP, 0.28 seconds. Hosted scanning on the new publication
revision remains a separate required gate.

Independent review is now available for the unchanged pipeline/test source at
publication head a668590:
[review and 12 passing focused tests](https://github.com/MulattoTech/MJobs/pull/941#issuecomment-6645975970).
That reviewer is a separate session which did not author this fix; this is
technical AI review, not human approval. Earlier NOT RUN wording above records
the original publication checkpoint. Required integration and deployment are
still unverified here; the parent acceptance remains PARTIAL.
