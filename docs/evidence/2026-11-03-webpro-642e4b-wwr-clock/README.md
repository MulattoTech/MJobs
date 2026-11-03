# WWR RSS date-fixture clock correction

## Scope and original hosted failure

This is a test-only correction for `WWR-RSS-DATE-FIXTURE-CLOCK`, owned by run `webpro-20261103T031738Z-642e4b` under claim `e66e520afa2b6b3d333fb15a449115041c681528`. It changes no application behavior, canonical acceptance, requirement status, RSS fixture or assertion.

Protected baseline: `63cb1042e08375f7bda1f9dad378209cca856167`.
Original PR 920 head: `9be83aa0906a24b04381773f01e69eb38efc01b9`.
Actual hosted candidate: `959407ad1237e0cad7ed9d3e402f229b4cace2e4`.
Failure: https://github.com/MulattoTech/MJobs/actions/runs/41600807060/job/125798817227

The retained hosted result is 1 failed, 931 passed, 9 skipped and 583 passing subtests in 865.41 seconds. The WWR source-intake test failed with `AssertionError: 2 != 3` at its original line 323. The affected test, literal RSS and date/query implementation match protected main; the 171 upload change did not modify them.

## Diagnosis and correction

The test froze only `services.discovery.search_semantics.datetime` at 2026-10-18 20:00 UTC. The `/jobs` endpoint independently calculated SQL date bounds using `apps.api.main.datetime.now()`. The literal third RSS posting is dated 2026-10-04 08:00 UTC. After 2026-11-03 08:00 UTC, the real API 30-day cutoff removes that row before the semantic filter runs; the remaining October 5 and October 6 rows explain the observed count of two.

The existing `_filtered(self, window, query_at=None)` helper already patches both clocks. The correction removes only the redundant local clock class/single-module wrapper and passes `query_at=query_at` to both the LAST_30_DAYS and LAST_24_HOURS calls. The literal RSS, three-row expectation, zero-recent-row expectation, raw/normalized timestamp, reopen and replay assertions are preserved.

## Executed native verification before commit

- Exact unchanged baseline test blob `790e999660e8f5e3d788ffd99a24b06cd05ead42`: 1 expected failure, 0 errors/skips, 1.76 seconds. The real API clock was not forced; the same `2 != 3` failure was observed.
- Corrected test blob `b1955b49794b34b637d38336fdc890f583f96cc6`: focused test 1 PASS, 0 failures/errors/skips, 1.59 seconds.
- The same corrected blob: complete existing module 9 PASS plus 8 passing subtests, 0 failures/errors/skips, 16.80 seconds.
- Each invocation reported one warning, retained in the console evidence without suppression.
- The driver checked six actual imported modules against the owned worktree and recorded their byte hashes. Existing cached native Python/dependencies were used without installation or environment modification. The existing transport interception supplied synthetic WWR data; no live WWR request, browser or paid API was used.

The executed commands, source-state distinctions and raw/published hashes are in `baseline-execution.json`, `corrected-focused.json` and `corrected-module.json`. `focused-driver.py` is the exact execution driver; `baseline-test-source.py` preserves the original committed source bytes. These receipts truthfully describe worktree execution before any new commit, not an execution attributed retrospectively to a future SHA.

## Boundaries

MJ-REQ-030 remains at its existing canonical status; no new acceptance credit is claimed. Hosted CI on the correction, broad product suites, real-provider behavior and deployment are not established here. Raw native logs/XML and private invocation/import-path mapping remain in ignored owned outputs. Published copies normalize private prefixes and the XML hostname, with separate hashes for raw and published bytes.
