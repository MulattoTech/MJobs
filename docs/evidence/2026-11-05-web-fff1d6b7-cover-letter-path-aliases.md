# Reviewed cover-letter directory alias admission

- Run: `web-20261105T012931Z-fff1d6b72f2d`.
- Requirement: MJ-REQ-171 / issue #172; related security review context #524.
- Baseline: `2e1c0072c2ea7241b832dc8842ce1cdc0d781cc1`.
- Exact tested source: `cc896af210ca713489ae63dd2534a8643a48897e`.
- Scope: one materialization boundary and its existing integration suite. No production data, external employer request, model invocation, migration, quota change or acceptance promotion.

## Defect and correction

Fresh approval resolved configured paths before checking containment. Resolution hid directory aliases, so a target inside the permitted root could be accepted even though saved-artifact replay already rejected aliases.

Four real synthetic Windows junction regressions failed on unchanged product source:

| Alias component | Baseline result |
| --- | --- |
| Configured output root | HTTP 201; reviewed documents created through the alias |
| Application directory | HTTP 201; reviewed documents created through the alias |
| Profile-version directory | HTTP 201; reviewed documents created through the alias |
| Reviewed destination directory | Document generation occurred before HTTP 422 / COVER_LETTER_REVIEW_ORPHAN_MISMATCH |

The implementation checks the complete lexical destination with the existing reparse-component guard before resolution and before staging or generation. It retains both containment checks, maps resolution errors to fixed COVER_LETTER_REVIEW_PATH_INVALID diagnostics, and refuses output/application/version/destination aliases without modifying them.

This is a reproduced storage-boundary inconsistency, not evidence of a live data disclosure or a remotely exploitable path.

## Executed verification

Baseline command:

`python -B -m pytest -q -p no:cacheprovider tests/integration/test_reviewed_cover_letter_prepare_binding.py -k test_fresh_review_rejects_aliased --tb=short --junitxml=outputs/web-fff1d6b7/cover-alias-baseline.xml`

Result: **4 FAIL**, 15 deselected, 10.26 seconds. Retained receipt: `outputs/web-fff1d6b7/cover-alias-baseline.log`.

Final command, run after committing the exact source above:

`python -B -m pytest -q -p no:cacheprovider tests/integration/test_reviewed_cover_letter_prepare_binding.py --tb=short --junitxml=outputs/web-fff1d6b7/cover-alias-committed.xml`

Result: **19 tests PASS, 10 subtests PASS, no failures or skips**, 83.83 seconds, exit 0. The earlier corrected working-tree run passed the same 19 tests/10 subtests in 40.72 seconds; these are repeated runs, not additive coverage.

The new cases verify fixed HTTP status/code, no document generation, unchanged target bytes/directories, unchanged database tables, identical reopened database state, and successful approval/idempotent replay after removing the synthetic alias. Existing promotion, stale/foreign context, concurrent approval, rollback, orphan reconciliation and artifact-tampering cases remain passing.

Other checks:

- Scoped Ruff: PASS after correcting one new explicit-subprocess-check warning and pre-existing import/nested-context style warnings in the two touched files. The initial five-warning result is retained in `outputs/web-fff1d6b7/ruff.log`.
- Catalogue, progress journal validation, dashboard freshness and whitespace: PASS.
- Strict commit provenance: PASS for both authored source/test files.
- Author source review: completed; no new authority, external access, dependency or schema introduced.
- Independent review: NOT RUN at publication.
- Current-candidate hosted checks: PENDING at publication.
- POSIX symlink branch: included in the same tests; NOT RUN locally. Hosted Linux execution remains a separate gate.
- Browser, PostgreSQL-specific concurrency and production deployment: NOT RUN for this slice.

Tests used Python 3.12 and the existing project environment read-only, with bytecode writes disabled. All databases, documents and junctions were confined to synthetic temporary directories. No heavy or browser slot was needed for the focused suite.

## Ownership and release

Claim: `refs/heads/webpro-claims/REVIEWED-COVER-LETTER-PATH-ALIASES` at owner `5590155d3db04b984e0dab4623f777d3ab08bf73`, acquired with HTTP 201 and matching readback at 2026-11-05T01:35:06Z. Ownership was revalidated before edits and publication.

Branch: `web/web-20261105T012931Z-fff1d6b72f2d/cover-path-aliases`. All 15 open PR scopes at selection were checked; none touched the source/test paths. The current browser-session, document/OCR and other integration claims are separate.

## Limits and rollout

Pre-existing configured directory aliases now fail closed on first approval, consistently with saved-artifact replay. No automatic relocation, unlink, chmod or repair is performed. An operator must select an unaliased configured path before retrying.

The check is admission-time protection. It does not claim to prevent a hostile actor replacing an ancestor after the check, prove Windows ACL isolation, remediate all historical artifacts, or satisfy the entire MJ-REQ-171 requirement.

Require fresh protected candidate checks and the separate integration/deployment locks. Rollback is a protected revert of this scoped source/test change; no database migration or restoration is needed. MJ-REQ-171 remains PARTIAL and canonical acceptance counts are unchanged.
