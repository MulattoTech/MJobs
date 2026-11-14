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

## Follow-up: canonical application identifiers and Linux verification

The initial hosted CodeQL analysis at published head `486730e26f0669821106103cb60e87fed3a30910` reported two new path-flow alerts ([129](https://github.com/MulattoTech/MJobs/security/code-scanning/129), [130](https://github.com/MulattoTech/MJobs/security/code-scanning/130)) from the route application identifier to the two newly split resolve calls. Analysis ID: 2099984732. The analysis completed without an analysis error; its two findings remain part of this record.

Follow-up source: **`e1a53ef4d7506ec533f0c36e70214c1308028163`**. Application IDs are generated by `identity(job_id, profile_id)` as lowercase SHA-256 digests. The materializer now checks that existing 64-character hexadecimal contract before any Store lookup and reconstructs the validated path token through integer conversion. Database identifiers, approval binding, lexical alias checks, containment checks and fixed error codes retain their intended behavior. No scanner rule, suppression, permission, dependency or CI configuration was changed.

A new regression exercises 12 invalid identifiers (wrong types, lengths, alphabet/case, traversal, path suffix, absolute Windows path and trailing newline), verifies the fixed path-invalid diagnostic, and proves that Store lookup and document generation are not reached. Existing successful approval and replay cases exercise canonical identifiers.

These results supersede the publication-time pending/local-POSIX statements above:

| Verification | Actual result |
| --- | --- |
| Windows focused pytest suite, source bytes committed at e1a53ef4 | **20 tests + 22 subtests PASS**, no failures/errors/skips, 62.18 seconds, exit 0 |
| Linux focused unittest suite, same source/test bytes | **20 tests PASS**, no failures/errors/skips, 36.191 seconds, exit 0 |
| Scoped Ruff and whitespace | PASS |
| Strict source-commit provenance | PASS for both source/test files |
| Fresh hosted CodeQL and protected checks | PENDING at this follow-up publication |

Windows command: `python -B -m pytest -q -p no:cacheprovider tests/integration/test_reviewed_cover_letter_prepare_binding.py --tb=short --junitxml=outputs/web-fff1d6b7/cover-alias-canonical-id.xml`.

Linux used Python's built-in unittest loader/runner for `tests.integration.test_reviewed_cover_letter_prepare_binding` in the already available `mjobs-micro:sha-cf882ef513b23429a3433ef690471bf7dff1fe21` image. The candidate worktree was mounted read-only; an explicit import-path assertion proved the tested service came from `/work/services/documents/reviewed_cover_letter.py`. The disposable container had no network or production mounts, all capabilities dropped, a 1 CPU / 1536 MiB limit and a 240-second process bound. Its synthetic SQLite/filesystem tests included real POSIX symlinks. Host source/test SHA-256 hashes were unchanged before/after and verified again before committing.

The first Linux attempt imported the candidate module but **ran no tests** because the runtime image has no pytest. That exit-1 setup failure is retained in `cover-alias-posix.log` and `cover-alias-posix-receipt.json`; no dependency or production image was modified. The built-in runner resolved that environment limitation. Final Linux receipts: `cover-alias-posix-unittest.log` and `cover-alias-posix-unittest-receipt.json`, under the same owned output directory as the Windows receipts.

Heavy slot `refs/heads/webpro-slots/heavy-01` was acquired for each isolated Docker attempt with separate HTTP-201 owner commits `11d09aaa9271ce24cf3bb9ec422c4884870dc32f` and `767b599bf6d650409b03e321f5798e819800e90d`. Both were released with HTTP 204 and confirmed absent. The completed unittest receipt ended at 2026-11-05T01:59:14Z.

An [independent source review](https://github.com/MulattoTech/MJobs/pull/950#pullrequestreview-5778681641) examined the original alias patch at 486730e2, found no blocking issue in that narrow guard, and required a source-grounded disposition of the CodeQL flows. It did not test or independently certify this follow-up commit. Integration, production deployment, hostile concurrent ancestor replacement and whole MJ-REQ-171 acceptance remain unproven by this slice. MJ-REQ-171 stays PARTIAL; no canonical counts were changed.
