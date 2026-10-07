# Durable dispatch and execution

Own deterministic PostgreSQL outbox -> Redis/RQ dispatch and one application
worker. Interfaces: enqueue_operation, dispatch_pending, execute_operation.
Requirements: MJ-REQ-004, 156, 158, 159 (scoped operation-boundary and
initial queue/restart slices).
Queue disconnect leaves QUEUED authority in the database. Execution claims use
conditional updates; duplicate jobs cannot both claim the same operation.
Exhausted and terminal worker failures are durable DEAD_LETTERED rows with
attempt timestamps and append-only audit events. Operators may explicitly retry
the same operation identity or discard it without erasing its failure record.
Submission-ambiguous applications remain in reconciliation and cannot be
retried from the dead-letter queue.
Redis and worker configuration are private environment values, never job args
containing credentials. Error persistence is category only. Tests: integration
API/queue tests plus Compose mock E2E. Broader running-operation recovery and
manual intervention resume remain PARTIAL; do not requeue a SUBMITTING
application for blind retry.

Worker claims are admitted under independent conservative category ceilings:
source collection (default 2, max 4), company discovery (default 1, max 2),
AI (default 1, max 2), document generation (default 1, max 2), and application
automation (default 1, max 2). Set `MJOBS_<CATEGORY>_CONCURRENCY` in the local
Compose environment. To use more than one source slot, scale the matching
Compose worker service (for example, `docker compose up -d --scale source-worker=2`).
Category admission and its PostgreSQL advisory lock are
shared across operation kinds and worker replicas; each RQ worker process still
executes one job at a time. Direct structured AI evaluations reserve the same
database-backed AI category before calling a provider, so separate API processes
share `MJOBS_AI_CONCURRENCY`. These inline evaluations are not RQ jobs and an
expired lease is marked `FAILED / LEASE_EXPIRED`, never requeued for blind retry.
Local SQLite and isolated PostgreSQL tests exercise serialized claims; the API
acceptance test uses two app instances against one SQLite database.

Every claimed durable task also receives a monotonic operation deadline. Its
cooperative `check` callback stops work at the configured budget, and a late
ordinary success is persisted as `DEAD_LETTERED / OPERATION_TIMEOUT`. The worker
does not add an automatic retry for a budget timeout. Operators can inspect
the bounded queue in Activity and choose a deliberate retry or discard.
Application confirmation remains authoritative; ambiguous submission
continues through `SUBMISSION_UNCONFIRMED` reconciliation and is never blindly
retried. This is a scoped MJ-REF-016 slice, not evidence that every external
operation category is complete.

For MJ-REQ-009, PERSONAL-mode workers block fictional discovery, mock-job
preparation and mock browser submission before executing queued fixture work.
The operation becomes BLOCKED with `FIXTURE_OPERATION_DISABLED`; application
state and any ambiguous submission evidence are preserved. Public source
collection, matching recalculation and real-job manual preparation remain
available. This boundary is tested in `tests.integration.test_worker_lifecycle`
while `.github/workflows/ci.yml` verifies an isolated personal Compose project.
