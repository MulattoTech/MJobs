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

Local evidence is recorded in
[`2026-10-07-dead-letter-queue.json`](../evidence/2026-10-07-dead-letter-queue.json).
Hosted workflows and reviewers are intentionally not run under the repository's
local-only delivery policy.
