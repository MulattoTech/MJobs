# Durable dispatch and execution

Own deterministic PostgreSQL outbox -> Redis/RQ dispatch and one application
worker. Interfaces: enqueue_operation, dispatch_pending, execute_operation.
Requirements: MJ-REQ-156, 158, 159 (initial queue/restart slice).
Queue disconnect leaves QUEUED authority in the database. Execution claims use
conditional updates; duplicate jobs cannot both claim the same operation.
Redis and worker configuration are private environment values, never job args
containing credentials. Error persistence is category only. Tests: integration
API/queue tests plus Compose mock E2E. Running-operation lease recovery and
manual intervention resume are still PARTIAL; do not requeue a SUBMITTING
application for blind retry.

For MJ-REQ-009, PERSONAL-mode workers block fictional discovery, mock-job
preparation and mock browser submission before executing queued fixture work.
The operation becomes BLOCKED with `FIXTURE_OPERATION_DISABLED`; application
state and any ambiguous submission evidence are preserved. Public source
collection, matching recalculation and real-job manual preparation remain
available. This boundary is tested in `tests.integration.test_worker_lifecycle`
while `.github/workflows/ci.yml` verifies an isolated personal Compose project.
