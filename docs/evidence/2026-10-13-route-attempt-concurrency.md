# MJ-REQ-221 / #222: shared-route application ownership

Area: Test Harness, with required Application Automation and durable worker integration.
Base: `0d8dca7c07c6d35ce61ffe2f2029e403dff4ccdc`.
Candidate: the commit containing this evidence; exact published revisions and hosted results are recorded in the PR.
Acceptance is **NOT_RUN** pending exact-revision PostgreSQL and hosted gates. No catalogue priority changes.

## Behavior and acceptance oracle

The former application-only reservation admitted two different applications on one route. The new regression first failed because neither contender received a route conflict.

Admission now locks the application and route in that order, validates current inputs, and checks durable RUNNING ownership from verified immutable start snapshots. It commits the reservation before Chromium starts. It does not hold a transaction throughout browser work, infer old ownership from mutable application data, or release ownership because a worker lease expires. Unknown legacy ownership blocks admission for review. Completed request replay returns the prior result without acquiring another browser.

Normal browser completion releases ownership. Receipt-confirmed recovery can release it only with the existing terminal recovered-worker fence; the original RUNNING attempt history stays unchanged. A confirmed label without that proof, an ambiguous receipt, or missing lease-recovery audit cannot release it.

The existing mock controls show why admission was blocked. Reload is read-only; retry requires a new reviewed request after the other owner finishes. No migration, external provider submission, paid inference, or real applicant data is used.

| Criterion | Automated oracle |
|---|---|
| Two workers cannot submit one route concurrently | Two independent subprocesses run `execute_operation` with capacity two, synchronize just before reservation, and use the real synthetic Chromium adapter. Exactly one returns COMPLETED, the other BLOCKED, and the fixture's submit endpoint count is exactly one. |
| Replay and intentional later work | Replaying the blocked operation starts nothing. A separately reviewed request after completion permits a second sequential submission. |
| Durable ownership | Independent database connections race; reopening the store and changing the owner's current route cannot steal its original reserved route. |
| Unrelated work and legacy safety | A different route can reserve; missing immutable ownership metadata fails closed globally. |
| Recovery | Controlled child `SystemExit` after a real fixture POST closes Chromium but suppresses the finish receipt to reproduce durable interruption. An unavailable receipt stays blocked. Verified read-only receipt recovery plus the terminal fence releases the route without changing original history. Removing recovery proof blocks it again. This is not an OS-kill/browser-orphan test. |
| Connected UI | Mounted production build -> API -> outbox -> fresh worker shows the blocked explanation, retains it across reload with no extra POST, then completes an explicitly requested new dry-run after owner release. |

## Local verification

- `python -m pytest -q tests/integration/test_route_attempt_concurrency.py tests/integration/test_route_attempt_concurrency_postgres.py tests/integration/test_application_attempt_history.py tests/integration/test_worker_attempt_identity.py tests/integration/test_application_confirmation_recovered_attempt.py tests/browser/test_route_attempt_ownership.py tests/browser/test_worker_attempt_recovery.py`: **42 PASS, five PostgreSQL SKIP, six subtests PASS**.
- `npm --prefix apps/web test`: **784 PASS** across 134 files.
- `npm --prefix apps/web run build`: **PASS**; existing large-bundle warning remains.
- Catalogue, progress journal, dashboard currency, repository policy and diff checks: **PASS**.
- `python -m pytest -q tests/unit`: **1,560 PASS, 1,018 subtests PASS**.
- Local Luna source review found the receipt-recovery release gap; it was corrected and tested. Follow-up found no additional blocker. This is not human review or external certification.

The PostgreSQL suite requires an explicit loopback disposable `MJOBS_TEST_POSTGRES_URL`, rejects connection-query overrides, creates a unique schema and uses independent process connections. Its five cases cover the same ownership/recovery contracts. It uses current metadata without a schema change; it does not independently prove Alembic migration behavior. Hosted integration supplies the disposable database and must execute these cases, not skip them.

Real external submissions remain disabled. Test fixtures and browser contexts are disposable. Unknown legacy RUNNING ownership deliberately requires review; there is no unsafe timeout-based steal or automatic retry.
