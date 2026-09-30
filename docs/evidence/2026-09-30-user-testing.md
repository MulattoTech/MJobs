# Verified local user-testing milestone

Local milestone: **PASS**. Full specification and release: **PARTIAL**. Remote
GitHub Actions: **BLOCKED_EXTERNAL**, because the native OAuth token lacks
`workflow` scope. No real external application was submitted or enabled.

The current branch is `codex/mj-pr-149-user-testing-mvp`, stacked on the worker
lifecycle branch. Main and the earlier three draft PRs are preserved. Scope and
remaining clauses stay in the 366-item capability matrix/backlog.

| Verification | Executed evidence | Result |
| --- | --- | --- |
| Domain, persistence, public source, API, worker and actual ATS browser contracts | [67-test suite](2026-09-30-mvp-verified-suite.log) | PASS |
| Final search/read-model changes | [4 affected tests](2026-09-30-mvp-search-affected.log) | PASS |
| Frontend tests, strict TypeScript and production build under Node24 | [build execution](2026-09-30-mvp-compose-build-final-reviewed.log), [final image export](2026-09-30-mvp-compose-build-final.log) | PASS |
| Existing Compose deployment / actual Chromium / lowercase search / factual revisions / download / dry-run / one mock receipt | [final UI proof](2026-09-30-mvp-ui-final.json), [execution log](2026-09-30-mvp-ui-final.log) | PASS |
| Fresh isolated project, new database/queue/document/receipt volumes, initial counts zero | [fresh-start proof](2026-09-30-mvp-fresh-start.json), [startup](2026-09-30-mvp-fresh-start.log) | PASS |
| Connected browser journey on fresh project | [fresh UI proof](2026-09-30-mvp-fresh-ui.json) | PASS |
| Actual public Greenhouse GET through deployed source worker; 20 native postings with raw hashes | [public-source proof](2026-09-30-mvp-public-source.json) | PASS |
| Live jobs displayed in browser, manual-only preparation, rejected real mock-run, visible cooldown error | [public browser readback](2026-09-30-mvp-ui-public-verified.json) | PASS |
| Redis outage, durable outbox, worker supervision, all-service restart, exact original application/operation/receipt once | [final recovery proof](2026-09-30-mvp-recovery-final.json) | PASS |
| Independent read-only witness: live authority, identities, four document records and source policy | [independent verification](2026-09-30-mvp-independent-verification.json) | PASS |

Each JSON proof records its command, timestamp, target and actual result. The
temporary fresh project was stopped after testing; its volumes were preserved.
The default deployment remains available at http://127.0.0.1:18700.

The final default browser application is
`a6de9ea88ec4ecb970783f5cecb53dc9b83d32c22850e7a9ae28155c92ac1e37`, with operation
`6ddcdad0b435c1936e0fe08046231a8949974cfe96e92657ae71a5cae9698368` and fictional
receipt `MOCK-a6de9ea88ec4ecb9`. Native confirmation count remained one after
the complete outage/restart test and independent GET verification.

Material findings were corrected before final validation: malformed provider
rows are isolated, invalid application URLs are unavailable rather than
constructed, source backoff cannot be shortened by configuration, stale queued
document generation cannot overwrite a current profile, pending facts are
excluded, ordinary rollback removes uncommitted files, and UI async reads are
guarded against profile/filter changes. Earlier red tests and harness failures
remain recorded separately; they are superseded by the exact final proofs.

The original source SHA256 remains
`1cf94181ae4b8e599063ada219759cb39b766f9d1bb7d860b99f4bb58adc0998`.
Catalogue validation passes with all 366 requirements retained. The PostgreSQL
upgrade preserves existing applications/documents and backfills immutable
profile-version metadata; fresh startup exercises all four migrations.

Limits: localhost single-user deployment, one configured public provider,
heuristic skill matching, factual fact-list templates, and manual-only real
application preparation. Broader provider/UI/AI/import/automation clauses,
manual submission status updates, challenge resume, and abrupt-crash file
retention remain open. See [USER_TESTING.md](../USER_TESTING.md) and
[RESUME.md](../RESUME.md).

The public provider interface follows the
[official Greenhouse Job Board API documentation](https://docs.greenhouse.io/job-board.html).
API schema and job content remain untrusted input to the bounded adapter.
