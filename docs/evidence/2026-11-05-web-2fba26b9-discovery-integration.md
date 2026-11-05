# Discovery reliability integration checkpoint

Run: web-20261105T024510Z-2fba26b9-255d-4f8c-904a-fe1a3c14ef0c.

## Purpose and source ownership

Integrate the separately authored and independently reviewed scheduler lifecycle correction (PR955 / MJ-REQ-160 / issue161) and malformed Remotive identity correction (PR948 / MJ-REQ-017.S08 / issue309) as one bounded protected candidate. No new product implementation is authored by this integration step. Both objective claims were acquired atomically and read back exactly. This run owns PR-955-INTEGRATION at 591179115cefc1dd5c10bec949f7e5b4989e47d7 and PR-948-INTEGRATION at 09dea867b6b43e0e3378485dd468345075a1b2b1.

Current main before this transaction: c6d499f391a8e092e06892d7fdea25cbfd177e53. Scheduler head: 8aa5e98ad92d5246d2ec06fcde3cca6deaa06846. Remotive head: e60804a9b1d44c727e8866c122dfef0800672c89. All eight original source/test/evidence/handoff files retain their exact original Git blobs. Both original histories and current main are preserved. No canonical acceptance, denominator, issue closure or Project/Fleet promotion occurs.

## Executed combined proof

The combined index tree before this evidence-only file was added is 67370cf87cbcfb2f2ea21da642a2bf38c267dc9c. The final merge commit preserves every tested product/test blob. Eleven focused modules ran once together: Remotive IDs, connected posting isolation, URL and body deadlines, existing collection, scheduler lifecycle, health, sanitized logging, transient retention, saved-search scheduling and validation.

Pytest output: ................................................................................................            [100%]
96 passed, 37 subtests passed in 16.97s

Three additional isolated reviewer probes PASS: Redis timeout cleanup preserves SystemExit; interrupted Redis close still disposes Store; failed tick retries and then closes resources with sanitized logs. Catalogue, progress journal, current dashboard and staged whitespace checks PASS. JUnit reports 133 cases, zero failures/errors/skips; subtest reporting may be included separately by pytest and is not additive to parent tests.

All fixtures are synthetic and isolated. Existing installed dependencies are read-only. No shared service, external source call, real application, paid model, autonomous worker, quota change, or heavy/browser slot is used. Live Redis outage, PostgreSQL-specific cleanup, browser, physical restart and production were NOT RUN locally.

## Independent review and limits

Scheduler source review: https://github.com/MulattoTech/MJobs/pull/955#pullrequestreview-5779216190
Remotive independent review: https://github.com/MulattoTech/MJobs/pull/948#issuecomment-6647357080

These are separate-session AI technical reviews of the original source, not human approval or independent review of a newly authored product patch. This integration preserves that source byte-for-byte and adds combined current-base verification. Earlier failures and prior-head results remain in the original evidence files and PR discussions.

Fresh protected CI on the final published candidate is still required. The global integration lock is released after publication and never retained while waiting for CI. Protected merge and guarded verified-main deployment remain separate. MJ-REQ-160 and MJ-REQ-017 remain PARTIAL; the Remotive child was already PASS.

## Rollback and next step

No schema migration or data rewrite. Any rollback must be a protected, reviewed revert assessed against intervening work. Next: validate current main/head/candidate, all five exact-head required gates, review threads and ownership under a short new integration transaction. Merge the combined PR955 only when ready; PR948 is included through original commit ancestry, not abandoned or implemented twice. Final receipts belong in both PRs.
