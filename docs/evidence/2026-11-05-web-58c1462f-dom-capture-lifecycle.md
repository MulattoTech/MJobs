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
| New protected CI, merge and production deployment | NOT RUN at initial evidence checkpoint |

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

At the initial source/evidence checkpoint, this change had author self-review
only. Later independent review is recorded in the follow-up below. Protected checks must pass on the actual PR candidate before
serial integration. Production remains a separate guarded release step.
Rollback is a code revert through the same protected process; no data migration
or restore is required. Existing broader diagnostic transport-envelope and
synchronous filesystem limitations remain as documented in the source.


## Follow-up: current-main candidate and shared scanner dependency

The normal merge refresh `36b810ce011630c4a69772256c74a98229a00b3e`,
tree `59e342d794840ed1338aa0a15c92f69c4f0f7f38`, includes protected main
`fc535d0eb684a03e9886fb36c480db9cacca8f5c`. Its focused unit/budget command
again passed 35 tests and 31 subtests (1.28s). Integration owner
`89df8da09a6eeb880a6f03046e9e095715107da2` was acquired by HTTP 201/readback
and released after normal publication. A prior 422 lock-acquisition conflict
made no merge or push.

[Independent source review](https://github.com/MulattoTech/MJobs/pull/946#issuecomment-6646197083)
found no scoped blocker at `25589043fc174b9f90ba4c8225dc1626d4645ca9`;
it did not independently execute tests or constitute human approval.
A [separate hosted old-code observation](https://github.com/MulattoTech/MJobs/pull/946#issuecomment-6646425330)
also retained the unawaited capture warning in PR939's successful unit run.
Neither observation is represented as protected CI on this final candidate.

The refreshed candidate's
[secret scan FAILED](https://github.com/MulattoTech/MJobs/actions/runs/41942702683/job/126967210786).
The downloaded redacted report contains exactly two findings from another
branch's immutable evidence history: commit
`c5339366e7372f9a5fca24c4c0410edacf8a6b2d`,
`docs/evidence/2026-11-05-web-30d19610-preview-error-privacy.md`, lines 45/46.
Protected main's [scan failed on the same pair](https://github.com/MulattoTech/MJobs/actions/runs/41941565134/job/126963403363),
blocking its deployment candidate. No finding refers to this diagnostic patch.

Both values were independently resolved as public Git source blob IDs:

| Source at c5339366 | Exact Git blob |
| --- | --- |
| tests/integration/test_browser_job_extraction_api.py | 1d8d8ae7964194af9cf91cf92b6eda1ef54bda5b |
| tests/integration/test_cheap_prefilter_api_scope_security.py | b4f13a866a81c63255a4ccecf5f7a3a11ae12e65 |

[Verification and coordination](https://github.com/MulattoTech/MJobs/pull/937#issuecomment-6646319529)
preceded edits. After PR933 released the shared-file claim, this run atomically
acquired `SECRET-SCAN-FINGERPRINTS` at
`fc3228a5663148046091335c677217e2912817a9`. Commit
`e9234126e1c0802d340abe99fefec5a8e082cee0` adds only the same five-line
append already reviewed in PR937. The complete resulting file matches Git blob
`cccac02db1fc375f6e9aff711f5525b15bd5a650` byte for byte. This narrows the
exceptions to those two exact immutable findings; it changes no scanner
detector, workflow, rule or path-wide policy.

```text
python -m pytest -q -p no:cacheprovider tests/unit/test_p0_evidence_secret_fingerprints.py tests/unit/test_secret_scan_sustain_176.py --tb=short
```

Result: 6 passed (0.26s); exact-blob and diff/provenance checks passed. The
diagnostic source and existing focused/browser proof are unchanged. Both failed
scan runs remain failed historical evidence. Fresh protected checks, serialized
merge and guarded deployment of the final published candidate remain separate.
