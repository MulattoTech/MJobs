# MJ-REQ-179: rejected native captures and phase deadline verification

## Revision and ownership

- Run: `web-20261104T235920Z-58c1462f-0bb0-4832-8e5b-483e6eb9d1f1`.
- Base main: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`.
- Source commit: `2b6d023064f6e519dd76daad11f91446801adbb3`.
- Source tree: `e74cc3e0d69109046cab6ad1bf4f6259efda4bb7`.
- Claim: `refs/heads/webpro-claims/MJ-REQ-179` at owner
  `7fa30f649f5bb58bcc25220b048905cb7568a6bc`, acquired by HTTP 201 and exact readback.
- [Scope and checkpoints](https://github.com/MulattoTech/MJobs/issues/180#issuecomment-6645902654).
- Scoped implementation and test repair only. Canonical acceptance is unchanged;
  the index already records MJ-REQ-179 PASS. This evidence makes no new acceptance,
  protected merge, deployment, or independent-review claim.

## Failure evidence and cause

PR #935 at `88f8e50299066b3f961c6fbe06fab37680afd65b` failed its
[unit CI job](https://github.com/MulattoTech/MJobs/actions/runs/41938465099/job/126952846842):
the stalled `world` phase expected a cancellation but observed none. The total
40ms budget can expire before that phase is entered. That run also emitted an
unawaited capture-coroutine warning. Its result remains FAILED: 1 failed,
3686 passed, 53 skipped, 3944 passing subtests. No unchanged rerun is used here
to relabel it as passing.

A deterministic new bridge-rejection regression failed in both RuntimeError and
asyncio cancellation subcases before the source change: the newly allocated
capture coroutine remained CORO_CREATED. The baseline reported two failed
subtests, one passing parent test and 18 deselected tests. The test explicitly
closes its retained baseline coroutine in cleanup so reproduction itself does
not leave an unawaited coroutine.

The first broader local Chromium run returned 15 passed, 1 failed and 14 passing
subtests (28.35s). The renderer-stall test expected exactly one screenshot but
observed zero. A single pristine-main run of that test PASSED (1 passed,
4 deselected, 3.05s), so the failure is intermittent, not a consistently reproduced
baseline failure. Current source attempts bounded DOM first; DOM can exhaust
the shared deadline, correctly preventing screenshot startup.

## Changes and preserved contracts

1. Retain the native capture coroutine while entering the sync bridge and close
   it only if its state remains CORO_CREATED. Started, suspended and completed
   captures retain their existing cleanup/cancellation path.
2. Start the module-local diagnostic test clock at the phase under test.
   asyncio waits and cancellation still use real event-loop time. The existing
   shared-total-budget expiry test remains separate and asserts that later
   phases and detach do not start after exhaustion. Only this module's time
   reference is patched; the global time module/event loop is not patched.
3. The real Chromium stall test now observes DOM admission and its caller-bounded
   deadline. Any screenshot must start before that same deadline with a positive
   timeout no greater than 120ms. If no screenshot starts, the caller budget
   must be exhausted. It still requires a result of None, elapsed time below
   0.75s, no published metadata, no page-world diagnostic evaluation, and a
   subsequent healthy independent capture with PNG, bounded DOM and private-value
   exclusion checks.

Production timeout values, cancellation limits, size/retention limits, sanitizing,
application submission policy and database schema are unchanged.

## Executed checks

All tests used an isolated worktree and private virtual environment with
repository-pinned pydantic 2.13.5/core 2.46.5 and Playwright 1.63.0. Shared/global
packages and user browser profiles were not changed. Synthetic local fixtures
only; no real applications or external model APIs.

| Check | Result |
| --- | --- |
| New bridge rejection baseline before production change | FAIL: two subtests, as described above |
| Focused unit and budget modules with RuntimeWarning/unraisable warnings as errors | PASS: 35 tests, 31 subtests, 1.44s |
| Real-loop cancellation/installed-protocol abort stress | PASS: 40 test executions, 200 phase cases, 7.865s |
| Broader three-module Chromium run before browser oracle correction | FAIL: 15 passed, 1 failed, 14 passing subtests |
| Single pristine-main stalled-renderer diagnostic run | PASS: 1 test, 4 deselected; intermittent failure not ruled out |
| Entire affected browser budget module after correction | PASS: 5 tests, 2 subtests, 5.71s |
| Catalogue, progress validate and dashboard --check | PASS |
| git diff --check and source commit provenance --strict | PASS |
| New protected CI, merge and production deployment | NOT RUN at evidence commit |

Focused command:

```text
python -m pytest -q -p no:cacheprovider tests/unit/test_browser_debug_capture.py tests/unit/test_fleet_t12_debug_capture_budget.py --tb=short -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning
```

Browser commands:

```text
python -m pytest -q -p no:cacheprovider tests/browser/test_debug_capture_privacy.py tests/browser/test_debug_capture_consumers.py tests/browser/test_fleet_t12_debug_capture_budget.py --tb=short -o faulthandler_timeout=90
python -m pytest -q -p no:cacheprovider tests/browser/test_fleet_t12_debug_capture_budget.py -k real_renderer_stall --tb=short
python -m pytest -q -p no:cacheprovider tests/browser/test_fleet_t12_debug_capture_budget.py --tb=short -o faulthandler_timeout=90
```

The middle command ran from a separate pristine-main worktree. The last command
ran after the browser-only oracle correction; the 15 successful broader tests
were not rerun without a relevant source change.

Stress harness (run using the same private Python):

```python
import sys
import unittest
from tests.unit.test_browser_debug_capture import NativeDomCaptureTests

names = (
    "test_each_stalled_protocol_phase_is_cancelled_without_background_tasks",
    "test_installed_protocol_abort_wait_is_bounded_in_each_native_phase",
)
suite = unittest.TestSuite(
    NativeDomCaptureTests(name) for _ in range(20) for name in names
)
result = unittest.TextTestRunner(stream=sys.stdout).run(suite)
raise SystemExit(not result.wasSuccessful())
```

The initial stress invocation used unittest's stderr stream; PowerShell treated
its progress dots as NativeCommandError and interrupted the wrapper. That
harness error is retained separately, not counted as a test pass. The corrected
harness changed only the output stream. A later remote commit-preparation call
timed out before executing; readback confirmed no commit/staging before the
operation was resumed.

## Resources, limits and next gate

Heavy-02 and browser-e2e-01 were acquired atomically, ownership rechecked, and
owner-matching refs deleted after each browser batch. The final release owners
were `34a888bdeea76b489588af562063ae8f3a70f488` and
`90a250b7cc4de26662465b39d9ec3586bf9d69eb`, respectively. No runtime service was
stopped and no production data was used.

This change received author self-review. No independent review of this authored
patch is claimed. Protected checks must pass on the actual PR candidate before
serial integration. Production remains a separate guarded release step.
Rollback is a code revert through the same protected process; no data migration
or restore is required. Existing broader diagnostic transport-envelope and
synchronous filesystem limitations remain as documented in the source.
