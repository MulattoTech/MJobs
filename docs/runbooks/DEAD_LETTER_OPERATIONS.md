# Dead-letter queue operations

The Activity page lists up to the 100 most recently dead-lettered operations for
the selected profile and shows the total matching count. Each row preserves the
operation ID, kind, failure category, cumulative worker-attempt count, last
attempt time, dead-letter time, and audit history.

Automatic retries are bounded by the worker's `MAX_ATTEMPTS` policy. A known
terminal task failure or exhausted lease moves to `DEAD_LETTERED`; the worker
does not retry it automatically. Review the recorded failure before choosing an
action:

- **Retry task** explicitly requeues the same durable operation ID and request
  identity. Its previous error and dead-letter timestamp remain available, and
  retry/dispatched attempts append audit events. A later claim increments the
  cumulative attempt count. Retry is available only for queue-backed operation
  kinds with a safe target state.
- **Discard** changes the state to `DISCARDED` while retaining the row, failure
  reason, timestamps, and audit events. Discard is irreversible through this
  interface.

For application automation, retry is rejected after confirmation, when review
is required, and whenever the application is `SUBMITTING` or
`SUBMISSION_UNCONFIRMED`. Reconcile the receipt or require manual review in those
states; never use queue retry to repeat an ambiguous submission. External
application submission remains disabled by default.

The queue endpoint is profile-scoped and read-only. Retry and discard are
separate explicit API actions. Database state is authoritative if the UI closes
or reloads while a worker is unavailable; the outbox dispatcher will publish
the queued operation when Redis is available again.

## Structured failure categories

Operation detail and profile-scoped progress responses expose an additive
`error_category` field. It contains one of the 15 categories defined by
`packages/domain/operation_errors.py`, or `null` when the producer has no exact
category. The original fixed `error` code remains available on operation detail.
Terminal attempt audit records and durable recovery metadata preserve the
category through process restart and API reload. Completed and cancelled
operations expose no failure category.

Classification uses exact producer codes, typed upload/document failures and
current application evidence. It never parses exception messages. A generic
challenge does not imply CAPTCHA, and unknown diagnostics remain unclassified.
Questionnaire evidence must match the application's profile, document and route
versions. Failure of this optional diagnostic read does not change an already
blocked operation into a failed or retryable operation.

The internal worker return preserves its historical `error_category` diagnostic
code for compatibility. Its additive `failure` object separates `code` from the
canonical `category`; consumers should use the public API category or this
structured object. Categorization does not change automatic retry policy.

Jobs marked REMOVED, EXPIRED, FILLED or CLOSED block document preparation and
new browser execution with `JOB_CLOSED`. The final submission admission checks
the locked job again, including a closure that occurs after browser startup.
Receipt reconciliation still runs before that initial closure guard, so prior
confirmed or uncertain submissions retain their recovery semantics and are
never replayed automatically.

Historical local queue evidence is recorded in
[`2026-10-07-dead-letter-queue.json`](../evidence/2026-10-07-dead-letter-queue.json).
Current publication follows the protected checks in
[`PR_PUBLICATION_POLICY.md`](../PR_PUBLICATION_POLICY.md).
