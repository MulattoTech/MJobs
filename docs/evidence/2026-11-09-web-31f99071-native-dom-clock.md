# MJ-REQ-179: native DOM malformed-response test oracle

## Result and scope

The targeted test slice passed at 552f7eae563876c2735287ec50cea197b5f2fb06: **36 tests and 33 subtests**.
Production source is unchanged. This is a test-oracle correction, with no new
full requirement acceptance or canonical matrix transition.

## Observed failure

[PR973 unit job](https://github.com/MulattoTech/MJobs/actions/runs/42291677981/job/128146410991)
tested merge b6cf60039f66c92c22b07d7f95da26047bb5f13d. The empty native-reply case
expected OMITTED_CAPTURE_FAILED but received OMITTED_TIMEOUT.
The tested production and test blobs match base 971ba931418fc271e7a652e27c288e1776448b6d.

The original validation-only fixture also used real elapsed capture time.
Production checks expiry after the sync bridge returns, before decoding.
Scheduling delay can therefore legitimately select the timeout result.
The logs do not identify the precise delay phase or establish a production bug.

## Assertions and preservation

| Criterion | Result | Evidence |
| --- | --- | --- |
| Malformed reply validation | PASS | Original input corpus and exact status retained under an isolated capture clock |
| Deadline boundary | PASS | Real sync bridge completes; one millisecond before expiry gives capture failure, exact expiry gives timeout |
| Privacy and cleanup | PASS | No decoding for rejected values and boundary cases; private text absent; full native phase order; zero pending tasks |
| Full requirement acceptance | NOT_RUN | No real browser or deployed journey; matrices unchanged |

Only debug_capture_module.time is replaced inside these tests. The real
asyncio clock, per-await timeout and cancellation protections remain active.
Severe event-loop stalls may still cause legitimate real-time expiry.

## Validation

- python -m pytest -q tests/unit/test_browser_debug_capture.py tests/unit/test_fleet_t12_debug_capture_budget.py: **36 passed, 33 subtests passed**.
- Catalogue, progress-journal validation, dashboard currentness, source-commit provenance and git diff --check: **PASS**.
- Independent static review (ref024_review): **no material findings**; reviewer did not run tests.
- Full local suite, build, Playwright/browser and deployed user journey: **NOT_RUN** under the heavy-operation resource gate.
- Hosted checks: pending publication. Existing all-history checksum scanner dependency is tracked separately; no scanner configuration changed here.

Test blob: e11c876c191c49e247d48b28781d0976e751054c.
Unchanged production blob: 85f4b5553a7994c850f11472dd65156415a59e32.
Commands, timestamps, environment and original failure are in the companion JSON.

## Integration and remaining work

This follow-up refs [issue180](https://github.com/MulattoTech/MJobs/issues/180)
and diagnoses one failure seen in PR973. It does not modify that peer's branch
or certify all of its checks. PR-only authority applies.

Observe checks on the exact published head. The scanner's historical checksum
exception already exists on PR977/978; preserve the current owner's claim.
Rollback is a revert of this test/evidence slice, with no runtime or migration change.
