# Preparation admission safeguards — combined integration qualification

## Scope and ownership

Run: `web-20261105T024510Z-1a0742d4c0c7`. Recorded 2026-11-05 UTC.

This run independently reviewed and tested the original fixes in [PR945](https://github.com/MulattoTech/MJobs/pull/945) and [PR950](https://github.com/MulattoTech/MJobs/pull/950), then mechanically combined them in the existing PR945 branch. It did not author the original behavior changes.

- MJ-REQ-155 / #156: canonically CLOSED, FILLED, EXPIRED or REMOVED jobs are not eligible for document preparation. Fresh admission fails before queueing; stale readiness remains stale; reopening and refreshing restores eligibility. Existing documents and tracking remain available.
- MJ-REQ-171 / #172: reviewed cover-letter preparation rejects malformed application identifiers and lexical destinations containing symlink/reparse aliases before file generation or application/document mutation. Canonical containment checks remain in place.

Both parent requirements remain **PARTIAL**. No acceptance-matrix, Project or Fleet status change is claimed.

Stable objective claims were created with HTTP201 and exact readback:
- `refs/heads/webpro-claims/PR-945-INTEGRATION` owner `c97370f2440f15a3b28c0d20ac53b8f3f05795bf`.
- Reacquired `refs/heads/webpro-claims/PR-950-INTEGRATION` owner `fd7028c27e09f0799fade633803376598cb4a734`.

Claims are temporary coordination, not permanent leases. The latest PR conversation and live refs determine current ownership.

## Exact combined source

Tested combined head: `fb8e68f45a23a69031adccbfbe43fa694a163a6f`.

It preserves history from:
- Protected main: `c6d499f391a8e092e06892d7fdea25cbfd177e53`.
- PR945 previous published head: `dfb4daf1e4fa53050e3af057683d7437ebb1a5bb`.
- PR950 published head: `91fe01ac51c2033f87743f5db326972bcbb1ec2b`.

All seven original PR945 source/test/evidence files were compared to the previous PR945 head and all three PR950 files to the incorporated PR950 head. Every original blob was unchanged. The combination adds no migration, dependency change or original source edit. This report is an additional evidence-only file.

The initial bounded integration owner `7fdf782b44cbba851764c28069b889c57197ce55` was acquired with HTTP201/exact readback and released with HTTP204/absence after the first launcher error. Hosted CI is never awaited under this run's integration lock.

## Executed combined verification

The first local helper failed before collection with `ModuleNotFoundError: services`, because executing a helper outside the checkout omitted the checkout from its import path. No product test ran and no push occurred in that attempt. The corrected helper explicitly prepended the owned checkout, confirmed both imported services resolved inside that checkout, and launched:

```text
python -B -m pytest -q -p no:cacheprovider tests/unit/test_pipeline_workbench.py tests/integration/test_pipeline_workbench.py tests/integration/test_reviewed_cover_letter_prepare_binding.py tests/integration/test_ai_tls_recovery.py --tb=short
```

Result at the exact combined head above: **55 tests PASS, 47 subtests PASS, zero failures and skips, 73.62 seconds**, process exit 0. The helper imposed a 180-second subprocess limit and completed in 76.640 seconds.

Also executed:
- `python -B -m tools.verification.catalogue` — PASS.
- `python -B -m tools.verification.progress validate` — PASS.
- `python -B -m tools.verification.progress_dashboard --check` — CURRENT.
- `python -B -m tools.verification.commit_provenance --commit HEAD` — PASS; merge commit changes no original source.
- `git diff --check` — PASS.
- Existing-PR authenticated publication preflight with separate reviewed old-head metadata and a candidate naming `previous_head_sha`, `--update-pr 945` — allowed, publication_checked=true, 21 projected development PRs, no errors. This is preflight evidence, not proof of GitHub check success or a push.

Private per-run temporary paths and synthetic fixtures were used. No live user record, external job application or paid AI provider call was made.

## Independent source reviews and prior focused results

These reviews concern source originally authored by other sessions. They are technical COMMENT reviews, not human approvals. The mechanical integration itself is not described as an independent review of this run's own work.

- [PR945 scoped review](https://github.com/MulattoTech/MJobs/pull/945#pullrequestreview-5779328221): canonical closed-state classification, fingerprint/API admission, fixed UI reason, preserved documents, reopening and profile isolation. Earlier native result: 32 tests and 19 subtests PASS at `dfb4daf1e4fa53050e3af057683d7437ebb1a5bb`, 26.56 seconds.
- [PR950 scoped review](https://github.com/MulattoTech/MJobs/pull/950#pullrequestreview-5779218512): identifier validation, lexical alias admission, canonical destination checks, error code, context binding and idempotent replay. Earlier native result: 20 tests and 22 subtests PASS at `204914d6c640294a1d217f021fa52ccace1e0153`, 78.56 seconds.
- Refreshed PR950 cover-letter plus TLS modules: 23 tests and 28 subtests PASS at `91fe01ac51c2033f87743f5db326972bcbb1ec2b`, 46.63 seconds.

Original frontend/Vitest/build/Chromium results remain attributed to the original PR945 executor and its evidence report. This run did not repeat those checks. The selected connected browser was unavailable, so an authenticated production journey is **NOT RUN**.

## Preserved hosted failure and bounded Linux diagnosis

The earlier PR950 [PostgreSQL/Redis workflow failure](https://github.com/MulattoTech/MJobs/actions/runs/41952373612/job/126999600572) is retained. The TLS recovery case `test_tls_cancellation_and_deadline_persist_without_uncertain_replay` exceeded its 0.9-second terminal bound for openai-compatible deadline: 1.130082617 seconds. The shard result was 1 failed, 892 passed, 27 skipped, 571 subtests. It was [reported to the bounded-operations issue](https://github.com/MulattoTech/MJobs/issues/5#issuecomment-6648360008). A subsequent local pass does not erase or explain that hosted failure.

A resource-slotted, disposable Linux diagnosis mounted the reviewed PR950 source read-only into a cached image, with network none, 1 CPU, 768 MB and 256 PIDs. The source import was asserted to originate from the mounted candidate. The built-in unittest TLS module passed 3 tests with no skips/failures in 6.491 seconds. Six synthetic loopback subcases measured:

| Provider fixture | Deadline terminal | Cancellation terminal |
| --- | ---: | ---: |
| openai-compatible | 297.9 ms | 209.3 ms |
| openai | 298.8 ms | 208.6 ms |
| azure-openai | 297.8 ms | 209.2 ms |

Image: `sha256:6d747da3f58adcd2e8516bfd8713fd54092c0ca05728e49a8ea19876e3bc6b4f`.
Heavy-slot owner `582784f27d903572745fca3e1d7b01d0c9f4c60f` was acquired via HTTP201/readback and released via HTTP204/absence. The run-specific container exited and was removed. No timeout assertion was weakened, no test was disabled, and no workflow gate was bypassed.

## Integration and release limits

At this report's creation the combined source is **TESTED LOCALLY / NOT MERGED / NOT DEPLOYED**. A source test, hosted required check, protected merge, deployment and parent acceptance remain separate facts. Current live PR/head/main/check state must be refreshed before publication or integration.

Required strict protected contexts include CI / required, Integration / PostgreSQL and Redis, Disposable Compose / mock end-to-end, Dependencies / introduced vulnerabilities, and Security / secret scan. The actual candidate must be qualified against the current main. Old-head green checks cannot satisfy a new candidate.

The existing guarded updater requires verified current main and its deployment checks, refuses a changed migration digest, verifies a quiescent backup, checks readiness and SHA, and retains prior-image rollback. This run has not invoked deployment at report creation. The observed pre-existing runtime at 03:13 UTC was `d6650e4bc9bfd784bd6e3a346c793c372f02518a`; that unrelated rollout is not attributed to this run.

Path admission does not establish protection against hostile ancestor replacement after admission or Windows ACL attacks. No migration or destructive recovery rehearsal was performed. Original issue gaps and all literal parent acceptance remain in force.

## Durable continuation

Use current ownership and PR conversation, then validate the exact head/base and actual proposed merge tree. Publish only a normal branch update following the existing-PR preflight; wait for fresh protected checks outside the global lock. Under a new short integration lock, recheck current main, heads, unresolved review threads and all gates before a protected merge. Verify incorporation of PR950 by commit ancestry and actual GitHub state before resolving any superseded PR. Deployment requires its separate lock and existing updater controls. At stop, release only this run's exact refs and record the remaining state.
