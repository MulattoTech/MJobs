# Source cancellation commit coordination — issue #770 / MJ-REQ-004

## Identity and scope

- Run: `web-20261105T000045Z-bc3539fc-7508-4222-b6fb-9ddb9c896784`.
- Baseline: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`.
- Tested implementation: `ad107a537e4541f166f79aa8b69c0f9a2a878f63`.
- Objective: `refs/heads/webpro-claims/770` at `e81382e9db2f88b00f3eb38d2a3d763c08d19194`; create-ref returned HTTP 201 and exact readback matched.
- Branch: `work/web-bc3539fc-770`.
- Product file: `workers/operations.py`.
- Regression file: `tests/unit/test_operation_cancellation_commit.py`.

## Reproduction and correction

Source request watchdogs continued polling SQLite while their own cancellation
transaction was trying to commit. A controlled regression holds a real SQLite
cursor open after the control SELECT, representing a descheduled polling reader.
The writer must then wait for that read lock. The unchanged baseline entered
this read during cancellation for all four source watcher names and all three
source operation kinds. Its twelve source commit samples took **0.547–0.594 s**,
beyond the unchanged **0.35 s** acceptance limit.

The existing AI watcher already paused until cancellation committed. Source
watchers now use that same commit-authoritative mechanism. An uncommitted or
rolled-back request never authorizes cancellation. Ordinary execution threads
still read the durable operation row.

A second failing regression showed that retrying cancellation after rollback
reused the old resolved-false event. Cancellation now has one outcome object per
transaction, so completion of an earlier attempt cannot resolve a newer attempt.
A waiter that follows a failed attempt to its successor retains its original
finite wait budget.

No existing deadline, timing assertion, source transport, schema, API route,
capability flag, or submission gate was changed.

## Verification on the tested implementation

The following commands used the existing pinned MJobs Python environment
read-only, with bytecode and pytest cache writes disabled. Stores, ports, threads,
and loopback servers were owned synthetic fixtures.

| Check | Observed result |
| --- | --- |
| `python -m pytest -q -s -p no:cacheprovider tests/unit/test_operation_cancellation_commit.py --tb=short` | **8 tests + 20 subtests PASS**, 7.98 s |
| `python -m pytest -q -p no:cacheprovider tests/integration/test_source_adapter_connect_cancellation.py tests/integration/test_source_http_socket_budget.py tests/unit/test_operation_control_reads.py --tb=short` | **42 tests + 25 subtests PASS**, 176.29 s |
| `python -m pytest -q -p no:cacheprovider tests/integration/test_durable_ai_evaluation.py -k 'cancel_closes_actual or cross_instance_queued_cancellation or cancel_during_provider_call' --tb=short` | **7 PASS**, 28 deselected, 11.19 s |
| `python -m tools.verification.catalogue` | PASS |
| `python -m tools.verification.progress validate` | PASS |
| `python -m tools.verification.progress_dashboard --check` | CURRENT |
| `git diff --check` | PASS |
| `python -m tools.verification.commit_provenance --commit HEAD` | PASS for the two authored source files |

The final controlled source-commit samples were **0.000–0.016 s as printed to
three decimal places**, with zero premature polling reads. These are synthetic
fixture measurements, not production latency guarantees. Rollback, retry,
superseded transaction outcomes, queued/terminal no-op behavior, and execution
thread durable readback passed.

The existing real socket suite retained its connect, public cancellation,
body-drip, header-stall, finite-deadline, positive-control, and negative-control
oracles. The AI cases use synthetic loopback HTTP servers and injected providers;
no external model service or paid inference was invoked.

## Preserved failed and intermediate observations

1. Initial test collection used system Python and stopped on incompatible
   `pydantic-core` / `pydantic` versions. **ENVIRONMENT ERROR**, no tests executed.
   The existing pinned environment worked; no shared package was changed.
2. The first pinned regression fixture looked for the wrong SQL table name and
   failed its observer readiness gate. **HARNESS ERROR**, not product evidence.
   The observer was corrected to use the actual mapped table name.
3. Corrected fixture on unchanged baseline: **16 failed subcases**, with five
   parent tests and four subtests passing; 25.08 s. The product failure was
   polling before the cancellation transaction resolved.
4. Initial source-watchdog correction passed ten focused tests and twenty
   subtests, including existing control reads. This intermediate result is
   superseded by the final implementation results above.
5. Retry after rollback failed before transaction-specific resolution was added:
   one failure, 14.75 s; the waiter returned the prior false outcome.

## Limits, release, and acceptance

This demonstrates a reproducible same-process SQLite polling failure mechanism.
It does **not** retrospectively prove that every historical hosted #770 timing
failure had this cause. Cross-process workers continue using durable control
reads; arbitrary connection, filesystem, commit, and runner-scheduling delays
remain outside this scoped guarantee.

At this checkpoint, exact-candidate hosted CI, independent review, protected
merge, production deployment, and live smoke checks are **NOT RUN / PENDING**.
Their later results belong in the PR with exact revisions and links.

Issue #770 remains open pending protected verification. Original **MJ-REQ-004
remains PARTIAL**. No canonical acceptance matrix, original denominator,
GitHub Project acceptance, or Fleet count was promoted.

Rollout is an ordinary application-code update through the existing guarded
updater after protected integration. There is no migration or configuration
change. Roll back through a protected revert or the previously retained release
image. No live database restoration was attempted.
