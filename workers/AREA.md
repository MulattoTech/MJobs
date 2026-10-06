# Durable dispatch and execution

Own deterministic PostgreSQL outbox -> Redis/RQ dispatch and one application
worker. Interfaces: enqueue_operation, dispatch_pending, execute_operation.
Requirements: MJ-REQ-004, 156, 158, 159 (scoped operation-boundary and
initial queue/restart slices).
Queue disconnect leaves QUEUED authority in the database. Execution claims use
conditional updates; duplicate jobs cannot both claim the same operation.
Redis and worker configuration are private environment values, never job args
containing credentials. Error persistence is category only. Tests: integration
API/queue tests plus Compose mock E2E. Running-operation lease recovery and
manual intervention resume are still PARTIAL; do not requeue a SUBMITTING
application for blind retry.

Every claimed durable task also receives a monotonic operation deadline. Its
cooperative `check` callback stops work at the configured budget, and a late
ordinary success is persisted as `FAILED / OPERATION_TIMEOUT`. The worker does
not add an automatic retry for a budget timeout. Application confirmation
remains authoritative; ambiguous submission continues through
`SUBMISSION_UNCONFIRMED` reconciliation. This is a scoped MJ-REQ-004 slice, not
evidence that every external operation category is complete.

For MJ-REQ-009, PERSONAL-mode workers block fictional discovery, mock-job
preparation and mock browser submission before executing queued fixture work.
The operation becomes BLOCKED with `FIXTURE_OPERATION_DISABLED`; application
state and any ambiguous submission evidence are preserved. Public source
collection, matching recalculation and real-job manual preparation remain
available. This boundary is tested in `tests.integration.test_worker_lifecycle`
while `.github/workflows/ci.yml` verifies an isolated personal Compose project.
