# MJobs checkpoint — control boundaries and domain persistence, 2026-11-02

## Verified baseline and acceptance

Live main and deployed revision at sprint discovery: **`0112ac4d5f62214925f43c2e5b68e01ad69e4322`**, merged by [PR889](https://github.com/MulattoTech/MJobs/pull/889). Its protected checks, production health and seven matching application images were read back. Canonical **P0 12/26 PASS** (4 PARTIAL, 10 NOT_RUN), **P1 106/116 PASS** (7 PARTIAL, 3 NOT_RUN); original **179/366 PASS**, 81 PARTIAL, 106 NOT_RUN. Separate reference authority **19/59 PASS**, 4 PARTIAL, 36 NOT_RUN. This source batch changes no acceptance result or criterion.

Prior accepted 162/167/175/208/267/275/288 and source PR886–889 remain established in [prior delivery evidence](../docs/evidence/2026-11-02-pro-resume/README.md); do not redo or relabel them as new sprint output.

## Current engineering batch

Branch **`work/pro-bounds-domain-20261102`**, native worktree **`C:\Tools\Dev\MJobs-pro-bounds-domain-20261102`**. [Current evidence](../docs/evidence/2026-11-02-pro-boundaries/README.md) preserves source hashes, failures, reviews and scoped test results.

- **004 PARTIAL:** transaction-local PostgreSQL/SQLite cancellation query limits, fixed unavailable-authority classification and a database-independent request socket clock. Real locked-database and socket-ownership regressions pass. Pool acquisition/connect/network black holes, arbitrary callbacks and other broad external-operation criteria remain unproven.
- **015 pending literal acceptance:** one immutable saved-search automation domain rule validates settings and execution. Malformed persisted rules no longer schedule operations or write audit/outbox records. Existing identities remain distinct.
- **174 pending literal acceptance:** actual ORM profile/version/fact reads preserve caller transactions and immutable history. Parameter binding and runtime least privilege are exercised on isolated PostgreSQL.

Latest reviewed Windows suite: **37 PASS / 0 SKIP / 54 subtests** (actual PostgreSQL, ORM, portable settings, locked reads and TLS recovery). Earlier Linux 30/10skip and 135/2fail/11skip scopes are retained separately. The wider Linux sweep had unavailable Chromium and a one-second TLS fixture dispatch failure; the TLS case passed unchanged in isolation and in native scope. Native actual browser-profile/reopen test also passed (1/0skip). Exact committed/proposed-merge checks, all five protected gates plus CodeQL, guarded delivery and complete015/174 criterion mapping remain required before acceptance. No skipped test is PASS.

## Review follow-up

[PR890](https://github.com/MulattoTech/MJobs/pull/890) is the single source integration path. Added018 source contract/policy checks and explicit job-feed fallback before merge. Independent review caught newer backoff overwrite, masked control-read errors, declaration exception leakage and news-feed promotion; all corrected and nine focused independent cases pass. Source branch remains `work/pro-bounds-domain-20261102`; the temporary source-contracts worktrees are staging/review copies only. Final native follow-up86PASS/13SKIP/31subtests. Previous0f/bf158daa native172/2skip and hosted results are retained as prior-head evidence; final revision checks and delivery still pending.018 and004 stay PARTIAL.

## Resume and next batch

Read the containing source PR receipt before acting; it records later exact revisions and gate results that cannot be self-contained in this source commit. Finish integration and guarded verification before updating canonical acceptance. Then consider **018**: unsupported ATS detection suppresses valid same-origin feeds, and current adapter strategy must be checked before egress and under the final import lock. **017** still lacks six named global integrations; official Dice MCP needs an explicit query contract. Do not accept either parent from enum declarations alone.

**008** prior evidence already exercises scoped RQ maintenance/redelivery; host restart and the complete original interruption journey remain unverified. **292** still requires its complete original journey. **171/172/224/269** remain substantive gaps. WWR conflicts remain safely blocked. Minimatch alert63 was already fixed. Paid inference and external employer submission remain OFF.

## Workstation and ownership

One integration owner. Preserve all unrelated work, including **13 dirty files in MJobs-fleet-recovery-20261026** and locked `.ao` worktrees. Do not reset unknown work, terminate unrelated processes, launch fleets or alter the 7% cutoff. Owned disposable PostgreSQL **`mjobs-pro-boundary-pg-20261102-01`** (ID `bdf1629274d0ef8b769a163992a5802ec85c395b7ac482cc5e47e455b2024322`, loopback42692, tmpfs) exists only for this batch and should be removed after exact verification. Never run destructive live restore tests.
