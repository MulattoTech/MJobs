# MJ-REQ-160: complete acquired worker Store cleanup

Run: `web-20261105T024512Z-1f7d3a290493`.
Requirement: [MJ-REQ-160 / issue 161](https://github.com/MulattoTech/MJobs/issues/161).
Delivery: [existing PR943](https://github.com/MulattoTech/MJobs/pull/943).
Source baseline: `ef4eb8cef1feeae76f633b749ff094f07fabd87b`.
Tested source: `e4601ea0eee9d1254e5649889daee404afb56d37`.

## Defect and correction

The earlier PR943 correction protects dispatcher startup and Redis acquisition.
Its documented earlier-startup limitation remained: after acquiring a Store,
configuration, registry, task-import, logger, Event or Thread construction errors
could leave that Store undisposed. KeyboardInterrupt/SystemExit during workspace
binding also bypassed the previous Exception-only cleanup.

One outer try/finally now owns disposal of the acquired Store. A private helper
retains the original startup order and existing dispatcher/Redis shutdown
behavior. The Store is disposed once after normal shutdown, configuration/setup
failure, or an early Python interruption. The original exception remains the
outcome when disposal succeeds. No retry ceilings, queue selection, logging
order, Redis socket limits or five-second cooperative thread join are changed.

The new tests use real isolated SQLite engines, engine-disposed events and a
persisted synthetic marker reopened through a separate connection. Store binding,
Redis/RQ and configuration boundaries are synthetic; no live worker or queue is
started. The inherited lifecycle suite separately exercises actual bounded
dispatcher threads.

## Preserved failure history

1. Initial fixture harness: pytest reported 17 failed / 3 passed. That attempt
   had an event-listener identity cleanup error and an unclosed SQLite read
   connection on Windows. It is HARNESS_ERROR and is not used as product proof.
   The original log remains in this run's private execution records.
2. Corrected harness on unchanged ef4eb8ce product source: 15 failing subcases;
   pytest summary `15 failed, 4 passed, 1 subtests passed`, exit 1, 1.83 s.
   All 15 failed at the missing single-disposal assertion. The normal binding
   failure and orderly-shutdown controls passed. No product source was changed
   before that reproduction.
3. Corrected working source: 42 tests / 32 subtests PASS, no failures/skips,
   11.00 s. The initial direct Ruff invocation found a new-test import-order
   issue plus four inherited worker diagnostics; the new test imports were
   sorted without altering assertions or suppressing rules.
4. Exact committed source e4601ea0: 42 tests / 32 subtests PASS, no failures/skips,
   10.05 s. Strict source provenance, catalogue, journal validation and dashboard
   freshness PASS. Staged whitespace PASS.

These executions overlap and are not additive feature coverage.

## Reproducible focused command

```text
python -B -m pytest -q -p no:cacheprovider -o faulthandler_timeout=30 tests/unit/test_worker_startup_resources_160.py tests/unit/test_worker_connection_lifecycle_160.py tests/unit/test_worker_queue_binding.py tests/unit/test_worker_startup_logging.py tests/unit/test_configuration_contract_sustain_276.py tests/unit/test_extension_point_contracts.py tests/unit/test_dispatch_connection_recovery_160.py tests/unit/test_retry_matrix_sustain_161.py --tb=short --basetemp <run-private-temp>
python -B -m tools.verification.commit_provenance --commit HEAD --strict
python -B -m tools.verification.catalogue
python -B -m tools.verification.progress validate
python -B -m tools.verification.progress_dashboard --check
git diff --check
```

Native Windows / Python 3.12.10 / pytest 9.1.1. The existing dependency environment
was used read-only, imports were rooted in the isolated worktree, bytecode/cache
writes were disabled, and temporary output was private to this run.

New test module Ruff: PASS. Full-file worker Ruff is not PASS: I001, BLE001 and
two LOG015 diagnostics remain. A same-configuration stdin comparison against the
unchanged baseline returns exactly those four diagnostics; this patch introduces
none. No lint/scanner suppression, CI policy or dependency changes were made.
Fresh protected hosted checks remain required.

## Earlier integration preparation, separately scoped

Before this new correction, the prior reviewed worker source passed 38 tests /
16 subtests on GitHub proposed merge e78e62bf, whose base was still 2e1c0072.
That is old-base evidence only. It also passed 38 / 16 on isolated preview
f0fb1558d7d98b833b63e43b8c99aa6424b6b5eb, tree
821e6ff470c124215ae1b9b40f34fad091ed78ee, combining protected main
d6650e4bc9bfd784bd6e3a346c793c372f02518a with ef4eb8ce.
Neither preview is a published integration or proof of the new source.

Bounded integration admissions encountered other owners. No integration lock
was overwritten, no protected branch was changed, and no production action was
taken during those attempts.

## Ownership and scope

- PR continuation: `refs/heads/webpro-claims/PR-943-INTEGRATION`,
  owner `7a2fc90ac5aa87fd003efd4107c382d08043bdfe`; HTTP201 and exact readback
  at 2026-11-05T02:47:54Z.
- Adjacent source correction: `refs/heads/webpro-claims/WORKER-STARTUP-RESOURCE-SAFETY`,
  owner `cf39b8bb1a14b6e680695b0ed3b61a9d59ffedb0`; HTTP201 and exact readback
  at 2026-11-05T02:55:48Z.
- The complete 21-open-PR path inventory found no other workers/main.py writer.
  This work is consolidated into the already owned PR943.
- Claims were revalidated before editing and committing. Final push/release
  receipts belong in the PR and this task's handoff.
- No heavy/browser slot or separate model/agent was used. No Fleet quota or
  emergency-cutoff setting was changed.

## Review, release and limits

The earlier independent reviews certify the original PR943 source, not this
new correction. Fresh independent review, current-main refresh and protected
candidate checks are PENDING. This run also independently reviewed the distinct
PR950 identifier-validation follow-up as source analysis only; it does not count
as review of this worker change.

Full MJ-REQ-160 remains PARTIAL. No acceptance matrix, denominator, Project/Fleet
completion or issue closure is changed. Linux execution, live Redis outage,
PostgreSQL-specific cleanup, browser behavior, physical restart and production
are NOT RUN by this source verification.

No guarantee is added for a Store constructor that fails before returning an
owned instance, failures inside engine.dispose itself, forced process/power loss,
or a dispatcher callback that outlives the existing bounded join.

There is no schema or data migration. Guarded rollout requires current protected
main and the separate deployment lock. A protected revert of e4601ea0 restores
the prior source and its earlier-startup cleanup gap while retaining the original
Redis/thread correction. Production deployment is not implied by publication.
