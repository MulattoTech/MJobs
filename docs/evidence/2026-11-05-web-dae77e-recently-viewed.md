# MJ-TH-015: closed-posting history correction and PostgreSQL rehearsal

Run: `web-20261105T000436Z-dae77e607c6e`. UTC date: 2026-11-05.
Issue: [#919](https://github.com/MulattoTech/MJobs/issues/919).
Existing PR: [#930](https://github.com/MulattoTech/MJobs/pull/930).

## Provenance and scope

Original implementation: `aa45df910b5e8c787678c63c3cd3728ec79283ea`.
Inspected base main: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`.
Lifecycle/API/UI correction: `0144e84fb9c9802fa9c900558b6d9f107d3b52d8`.
PostgreSQL test source: `e6e3a15b4d362b08eff99240a7f5002ae37e726b`.
The tested working-tree source/test blobs were committed unchanged in those commits.
This evidence commit changes no application behavior.

Atomic objective refs `webpro-claims/MJ-TH-015` and
`webpro-claims/PR-930-INTEGRATION` were both created with HTTP 201 and read back
at `5a8829aa11ca4d38a3d4e6798f88b617d449031a`. Their final release is recorded in the PR.
A unique worktree preserves the original feature commit through an ordinary
merge with main. No shared main.tsx edits, canonical acceptance changes, separate
model calls, external applications, or live database writes were performed.

## Reproduced defect and correction

The original response considered any retained catalog title available. A canonical
posting marked CLOSED, FILLED, EXPIRED, or REMOVED therefore remained actionable
in recent history. The new regression failed for all four states and normalized
lowercase/whitespace CLOSED before the fix (five failing subtests).

The response now uses the existing canonical lifecycle parser and CLOSED_STATES.
Closed postings retain their real title/company, carry `NO_LONGER_OPEN`, and
render without an open button. An absent catalog row carries `MISSING` with no
invented metadata. Active/unknown catalog entries remain readable; no inference
of employer-confirmed availability is added. The client rejects contradictory
availability/reason/metadata. Listing does not change visit order/timestamps,
tracking dispositions, audit rows, or application counts.

## Executed verification

| Command / scope | Result |
| --- | --- |
| `python -m pytest -q tests/integration/test_recently_viewed.py -k closed_canonical` before correction | Expected FAIL: five terminal-status subtests; original behavior reproduced |
| `python -m pytest -q tests/integration/test_recently_viewed.py tests/integration/test_recently_viewed_migration.py` | PASS: 7 parent tests and 10 subtests, 0 skipped; 104.25 seconds |
| `node node_modules/vitest/vitest.mjs run --maxWorkers=1 src/recently-viewed-client.test.ts src/recently-viewed-panel.test.tsx` | PASS: 8 tests in two files |
| `node node_modules/typescript/bin/tsc --noEmit` | PASS |
| ESLint on the four changed client/panel source and test files | PASS |
| `node node_modules/vite/bin/vite.js build` | PASS: 141 modules; existing large-bundle advisory remains |
| `python -m pytest -q -o faulthandler_timeout=40 tests/integration/test_recently_viewed_postgres.py` | PASS: 2 tests on real PostgreSQL 17, 0 skipped; 2.94 seconds |
| Catalogue, progress validation, dashboard check, diff check and commit provenance | PASS |

The PostgreSQL rehearsal used an owned ephemeral container, one CPU, a 768 MiB
limit, a loopback-only random port, a disposable mjobs_test database and fresh
UUID schemas. The fixture rejects other database destinations. It upgrades from
`20261025di01` to `20261104rv01` over existing synthetic profiles, jobs and
workspace identity; verifies those records remain unchanged; reopens both
profiles' histories; refuses a populated downgrade without losing records;
clears only the selected profile; and permits an empty downgrade while preserving
legacy data. A second case proves eight concurrent explicit visits serialize
into two deduplicated rows with a maximum visit order of eight, and that an
expired-job read leaves visits and application counts unchanged.

Both heavy-slot acquisitions used HTTP 201 / exact readback. Each owned container
was removed and each exact slot released with HTTP 204. No heavy slot remains
held by these rehearsals.

## Preserved failures and limits

- The first PostgreSQL fixture attempted to run the new Store workspace-binding
  guard before adding its target table. Both setup cases correctly failed with
  missing `profile_job_views`. The fixture was corrected to seed the historical
  source-schema workspace directly; no application/schema guard was weakened.
- PowerShell treated the existing npm/Node prerelease compatibility warning as a
  command error. Subsequent direct Node invocations used the successfully
  installed isolated dependencies and returned verified exit codes.
- The first rehearsal wrapper could not print a Unicode Vite success glyph in
  the Windows console. The build's exit-0 receipt and log were recovered; cleanup
  and exact-slot release succeeded. This was a wrapper output error.
- SQLite, PostgreSQL, client rendering and build results are distinct. A mounted
  browser journey, full guarded backup/migration driver rehearsal, protected
  refreshed-candidate CI, merge, production rollout and acceptance remain
  NOT_RUN at this source checkpoint.
- Existing `cli.guarded_migration` supplies an offline, backup-bound, one-use
  operator route. The scheduled updater still correctly refuses a changed
  migration digest. Do not merge into the release stream until a guarded
  migration deployment window, backup verification and rollback plan are ready.
  No guard removal or live downgrade is authorized by this evidence.
- Retention remains at most 100 unique rows per profile; 90-day filtering occurs
  on reads, with physical expiry pruning on the next explicit visit. No continuous
  cleanup scheduler or new multiuser authentication layer is claimed.

Original 366 requirements, P0/P1 denominators and separate reference/TalentHop
authorities remain unchanged. This run does not mark MJ-TH-015 ACCEPTED.
