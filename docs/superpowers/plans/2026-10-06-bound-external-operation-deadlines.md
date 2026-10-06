# Bound External Operation Deadlines — Initial Worker Slice

## Goal

Advance open P0 issue #5 / MJ-REQ-004 without treating the merged narrow worker lifecycle work as full completion. Keep its broad requirement `NOT_RUN`/`PARTIAL` until timeouts, captured output, controlled cancellation, retry policy, and error classification have evidence across every applicable operation category.

## Current code boundary inventory

- HTTP: the Greenhouse adapter uses an explicit per-request timeout and payload cap; the employer HTTPS client has DNS/socket deadlines and cancellation callbacks; the mock client is loopback-only and configured with an HTTP timeout.
- Browser: the mock ATS uses bounded Playwright launch/navigation/selector defaults and catches failures into review states. Cancellation checks run between major form stages, but not while waiting through the Greenhouse adapter's request-rate delay.
- OCR/process: OCR subprocesses use a hard timeout, bounded resources, no shell, discarded raw stdout/stderr, and safe public warning categories. No AI provider request implementation exists yet.
- Parsing: resume inputs have size/format constraints and OCR has page, pixel, output, and time limits; pure parsing still needs a focused bound audit.
- Durable worker: leases and recovery cap abandoned attempts, but the operation timeout currently affects only lease length. `execute_operation` has no monotonic end-to-end deadline check before recording success.
- Source collector: provider errors are categorized and rate-limited failures persist backoff; its inter-request rate wait uses one `sleep()` and cannot observe cancellation during that wait.

## First coherent vertical slice

Enforce one monotonic budget in the durable worker execution boundary and make the provider rate-limit wait cooperative. On deadline expiry, safe worker operations terminate as `FAILED / OPERATION_TIMEOUT`; no generic retry is added. Submission ambiguity remains governed by the existing `SUBMISSION_UNCONFIRMED` reconciliation path. The Greenhouse wait polls cancellation with a small bounded interval and must stop before issuing the next HTTP request.

## Files and acceptance oracles

- `workers/tasks.py`: derive an operation deadline after claim, include it in the existing injected `check` callback, perform a final check before recording ordinary success, and persist a stable timeout category.
- `tests/integration/test_worker_lifecycle.py`: use a short injected test budget and deterministic monotonic clock to prove an over-budget worker cannot be recorded COMPLETED and that the durable error category is `OPERATION_TIMEOUT`.
- `services/discovery/sources/greenhouse.py`: replace the single potentially long throttle sleep with cancellation-aware bounded waits while preserving the configured minimum request interval.
- `tests/integration/test_greenhouse.py`: use a deterministic clock and cancellation callback to prove cancellation during a rate wait prevents the following network request.
- `workers/AREA.md`, `services/discovery/sources/AREA.md`, and issue evidence: document this limited behavior and its remaining coverage boundaries.

## TDD and validation sequence

1. Add both regression tests and run them to observe the expected failure: a late worker operation is reported as completed, and cancellation is not checked until after the rate wait/request.
2. Implement the deadline and cooperative wait with no generic retries or output disclosure.
3. Run the two focused integrations, worker/source regressions, full local Python/browser suite, frontend build if source behavior changes the UI contract, and repository validators.
4. Inspect diff and negative/recovery behavior, especially cancellation before submission and post-submit ambiguity.
5. Record exact hashes, timestamps, commands, limitations, and evidence. Keep MJ-REQ-004 open/PARTIAL until the remaining operation categories and their output/retry behavior are audited and locally verified.

## Known unimplemented boundaries

AI request timeout/retry evidence cannot be produced until an AI provider path exists. No paid/hosted model calls, GitHub Actions, hosted review, cloud test, or external application submission is authorized.
