# MJ-TH-015: recently viewed lifecycle, browser and migration verification

Run: `web-20261105T000436Z-dae77e607c6e`. UTC date: 2026-11-05.
Issue: [#919](https://github.com/MulattoTech/MJobs/issues/919).
Existing PR: [#930](https://github.com/MulattoTech/MJobs/pull/930).

## Provenance and scope

Original implementation: `aa45df910b5e8c787678c63c3cd3728ec79283ea`.
Inspected base main: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`.
Lifecycle/API/UI correction: `0144e84fb9c9802fa9c900558b6d9f107d3b52d8`.
PostgreSQL test source: `e6e3a15b4d362b08eff99240a7f5002ae37e726b`.
Browser test source: `ef009a775e0bdde9637a5df2e70ae303f8e0f961`.
Ordinary branch refresh includes protected main `fc535d0eb684a03e9886fb36c480db9cacca8f5c`
at merge `00d224fc000b1248832082fe8dec80845a85d4ab`; its five-file delta contains
Greenhouse/Recruitee adapter cancellation code, unit tests and evidence, with no
changes to the recently viewed or guarded-migration inputs tested here.
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
| `python -m pytest -q -o faulthandler_timeout=40 tests/browser/test_recently_viewed.py` | PASS: 1 isolated Chromium journey, 0 skipped; 9.09 seconds |
| Full stock `python -m cli.guarded_migration` rehearsal on a disposable project | BLOCKED before PostgreSQL startup: Docker default network pools exhausted; full command NOT_RUN |
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
- SQLite, PostgreSQL, client rendering/build and isolated Chromium results are
  distinct. User-connected Brave could not initialize because its originating
  browser context was unavailable. Full guarded backup/migration driver,
  protected final-candidate gates, merge, production rollout and acceptance are
  not established by these local results.
- Existing `cli.guarded_migration` supplies an offline, backup-bound, one-use
  operator route. The scheduled updater still correctly refuses a changed
  migration digest. Do not merge into the release stream until a guarded
  migration deployment window, backup verification and rollback plan are ready.
  No guard removal or live downgrade is authorized by this evidence.
- Retention remains at most 100 unique rows per profile; 90-day filtering occurs
  on reads, with physical expiry pruning on the next explicit visit. No continuous
  cleanup scheduler or new multiuser authentication layer is claimed.


## Isolated connected UI journey

The new browser test runs the real local API and built web app against an owned
temporary SQLite database and two synthetic profiles. It starts its own headless
Chromium context and rejects every request outside its assigned loopback origin.
It proves explicit card opens record visits, repeated opens deduplicate, newer
visits sort first, profiles remain isolated, CLOSED postings retain honest labels
without an open button, and history survives reload. Canceling clear preserves
history; confirming clear affects only the selected profile. A 390-by-844 viewport
checks panel visibility and the explanation for disabled saved-only filtering.
This is a visibility check, not a comprehensive mobile/accessibility audit.

The final database has zero applications and zero disposition/note audit events;
the external-request collection is empty. The tested browser file's workstation
SHA-256 is `3c84977b6d7c2e5e8e337a9c4a484dd35957decfafaebb07c90936f66d221750`.
The final source was committed without behavioral edits.

The first browser-slot attempt lost an atomic create race and ran no test; its
owned heavy slot was immediately released. A later fresh owner acquired both
heavy-01 and browser-e2e-01 with HTTP 201 and exact readback. After the passing
journey, both exact refs were deleted with HTTP 204. No user browser profile,
production data or external employer page was used.

## Full guarded rehearsal: retained setup refusals and blocker

The native runner was scoped to fresh, uniquely named Compose projects and
generated synthetic credentials. Its intended checks were unauthorized-upgrade
refusal, the unmodified backup-bound upgrade, sentinel preservation and native
restore into a separate new database. **Those end-to-end checks did not run.**

1. Attempt 1 failed while preparing its private directory because an inherited
   PowerShell module search path prevented the Security module from loading.
   The isolated child environment was corrected; no product code changed.
2. Attempt 2 used canonical source archived from
   `97f541dc20dd2f0e09da473de26ff0cf4edf335e`. Its source archive SHA-256 was
   `54bfb2fc09a628e85558d5d6aae179f07f41ef261b086b1caa44b133957b69da`.
   Host and disposable image migration-input hashes matched
   `f7a567728526113f8a37c62f92ee20c11a239d6b2e603a514a322fa9a57a3884`.
   The unchanged NTFS guard then rejected the selected backup-parent ancestry;
   PostgreSQL was not started.
3. Attempt 3 selected a fresh protected NTFS directory whose ancestry passed the
   stock guard. The source archive and host/image migration-input hashes again
   matched. Docker then refused the new private network because all predefined
   address pools were fully subnetted. No PostgreSQL container or application
   writer started. Exact project cleanup read back **0 containers, 0 volumes,
   and 0 networks**; the run-owned test image tag was removed.

Each attempt acquired a fresh heavy-01 owner via HTTP 201 and matching readback,
then released only that owner via HTTP 204. An intervening read-only connection
recovery found no active RDC process sessions. No foreign Docker network,
container, volume, address pool, permission setting or coordination ref was
modified. Private setup logs and sanitized receipts preserve these failures.
The passing direct PostgreSQL migration tests above must not be relabeled as a
passing full guarded-driver rehearsal.

## CI and release boundary

At published source `97f541dc20dd2f0e09da473de26ff0cf4edf335e`,
[CI run 41940666958](https://github.com/MulattoTech/MJobs/actions/runs/41940666958)
reported PASS for repository policy/docs, Python unit/integrity, web tests/build,
and its required aggregate. Dependency review, CodeQL and
[disposable Compose](https://github.com/MulattoTech/MJobs/actions/runs/41940666922)
also passed. A later read confirmed all three PostgreSQL/Redis integration shards
and their required aggregate PASS on that published checkpoint.

The required [secret scan](https://github.com/MulattoTech/MJobs/actions/runs/41940666986)
failed. Its bounded redacted artifact contains two generic-key findings in
`docs/evidence/2026-11-05-web-30d19610-preview-error-privacy.md` from another
branch, not this feature's changed files. The existing
`SECRET-SCAN-FINGERPRINTS` owner is handling that scope. This run did not change
scanner rules or suppress findings. These earlier checks do not certify the
later browser/evidence/main-refresh commits; fresh protected candidate checks
are required.

The read-only release preflight observed production at
`42ba63525c93e26bfe7168c4875a21fee4a5c6cd` in PERSONAL mode with healthy
application services. The Cloudflare tunnel was healthy, ingress required
Access, the configured policies contained no observed bypass/everyone rule,
and an unauthenticated public request returned HTTP 403. No signed-in production
journey is claimed.

**OPEN / NOT MERGED / NOT DEPLOYED / NOT ACCEPTED.** The next integration owner
must revalidate claims, refresh main and the exact merge candidate, clear every
required protected check, and arrange a supported guarded release transaction.
The stock scheduled updater compares migration digests and refuses this change;
a passing standalone migration alone would not authorize bypassing that refusal.
Repeat the full native rehearsal when isolated Compose networking is available.
Keep the verified backup and an explicit recovery plan before any live schema
change; never automatically downgrade populated visit history.

Original 366 requirements, P0/P1 denominators and separate reference/TalentHop
authorities remain unchanged. This run does not mark MJ-TH-015 ACCEPTED.
