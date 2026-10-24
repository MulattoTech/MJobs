# MJ-REQ-012: service architecture evidence and remaining gap

References [issue #13](https://github.com/MulattoTech/MJobs/issues/13) and the
[service architecture specification](../spec/areas/governance_safety.md).
Reviewed source baseline: `8d3a97ffb248de1d8994dec5e919225e1ac9fc12`.
Evidence captured on 2026-10-24 UTC in the isolated architecture-review worktree.

**Verdict: PARTIAL.** Separate consumers and category limits are implemented and
the focused regressions below pass. The literal connected claim that a blocked
application browser cannot block job discovery is **NOT_RUN** in this review.
No production behavior, tests, canonical acceptance state, or deployment was changed.
This document is not an issue-closure or whole-product PASS assertion.

## Contract and observed topology

The requirement permits worker consolidation; it does not require every example
service name to exist. It does require logical queue separation and discovery
progress independent of browser-worker occupancy.

| Compose service | Consumed queue | Operation category / purpose |
| --- | --- | --- |
| `worker` | `mjobs-application` | Application automation: mock apply, form review, dry run |
| `source-worker` | `mjobs-discovery` | Source collection (`DISCOVER`) |
| `company-discovery-worker` | `mjobs-company-discovery` | Company discovery and company-source refresh |
| `ai-worker` | `mjobs-ai` | Queued AI evaluation |
| `document-worker` | `mjobs-document` | Preparation, queued resume extraction/comparison |
| `scheduler` | Not an RQ consumer | Due-work scheduling |

Source anchors:

- [compose.yml](../../compose.yml): the five workers have explicit
  `MJOBS_WORKER_QUEUE` values; PostgreSQL and Redis are shared dependencies.
  Runtime containers have CPU/memory ceilings, not a reservation of host capacity
  for discovery. Application worker does not join the source-egress network.
- [workers/main.py](../../workers/main.py), `main`: validates the configured queue
  and constructs `Worker([queue], ...)`, not a worker subscribed to all queues.
- [company discovery entry point](../../workers/company_discovery_worker.py): pins
  the shared runner to the company-discovery queue.
- [workers/operations.py](../../workers/operations.py), `QUEUES`,
  `OPERATION_CATEGORIES`, `_lock`, `claim`: independent category counts and
  PostgreSQL category-specific advisory locks. Browser capacity does not count
  against source-collection capacity. SQLite's write lock is broader than these
  PostgreSQL category locks, so SQLite evidence is not equivalent concurrency proof.
- [runtime health](../../services/runtime_health.py): readiness checks queue-worker
  presence/heartbeat and scheduler freshness. A healthy heartbeat does not prove
  that a particular discovery request can finish under browser load.

The frontend/API and scheduler do not replace the durable outbox. Queue transport,
database state and worker execution must be observed separately when certifying a
connected operation. Browser submission remains OFF for this work.

## Executed regression evidence

Environment: Windows, Python 3.12, existing project virtual environment at
`C:/Tools/Dev/MJobs/.venv/Scripts/python.exe`. No dependency installation, service
startup, application submission, or existing personal database access was needed.
Commands below ran from the worktree root against the baseline source above.
The PR must separately record its final tested commit and hosted checks: a source
baseline or an earlier green CI run cannot certify a later revision.

### A. Queue topology, limits and health rejection: PASS

```powershell
python -m pytest -q tests/unit/test_worker_queue_binding.py tests/unit/test_operation_concurrency.py tests/unit/test_runtime_health.py tests/unit/test_compose_health_checks.py tests/unit/test_compose_modes.py -p no:cacheprovider
```

Observed: **27 passed, 70 subtests passed**, 8.96 seconds.
This includes single-queue worker construction, distinct rendered Compose worker
bindings, independent limit configuration, missing/stale heartbeat rejection, and
invalid/unhealthy container-state rejection. Compose tests render configuration;
they do not start or inspect a connected deployment. RQ/health dependencies in
these unit tests are mocked. This is not a blocked-browser integration proof.

### B. Safe admission and nonblocking source import: PASS

```powershell
python -m pytest -q tests/integration/test_company_discovery_rq_acceptance.py::CompanyDiscoveryRQAdmissionTests -p no:cacheprovider
```

Observed: **2 passed, 6 subtests passed**, 3.28 seconds.
The controls reject non-disposable Redis targets before I/O and exercise a bounded
child-process source import with durable SQLite reopen while company consumption
and employer egress are absent. This proves the stated import path only, not live
Redis delivery, PostgreSQL behavior, or progress while an application browser runs.

### C. Durable SQLite capacity and duplicate-claim controls: PASS

```powershell
python -m pytest -q tests/integration/test_worker_lifecycle.py::WorkerLifecycleTests::test_capacity_is_independent_and_a_duplicate_cannot_claim tests/integration/test_worker_lifecycle.py::WorkerLifecycleTests::test_company_discovery_kinds_share_one_category_capacity tests/integration/test_worker_lifecycle.py::WorkerLifecycleTests::test_concurrent_application_workers_share_capacity_across_store_instances -p no:cacheprovider
```

Observed: **3 passed**, 3.51 seconds.
These cover duplicate rejection, category saturation and independent admission,
including concurrent application claims across store instances. They call the
claim boundary; they do not start RQ consumers or execute a blocked browser.

## Connected-runtime gap: NOT_RUN / allocation required

No isolated connected-runtime slot, PostgreSQL/Redis target or Linux worker
environment was allocated for this session. No shared/live containers were used.
The existing [company RQ acceptance test](../../tests/integration/test_company_discovery_rq_acceptance.py)
contains a Linux-only real-worker case with explicit disposable Redis admission.
Its wrong-queue control proves that source workers cannot consume company jobs;
it does not hold an application browser open while discovery finishes. Its
private durable store is SQLite, not PostgreSQL.

The [PostgreSQL concurrency suite](../../tests/integration/test_operation_concurrency_postgres.py)
also requires an isolated database. It tests category claim concurrency, not
end-to-end browser-versus-discovery scheduling. Neither suite was used as a
substitute for the missing connected oracle, and neither unexecuted path is PASS.

Required bounded acceptance scenario, subject to separate test ownership and slot:

1. Launch source and application consumers from one exact image/source revision
   in an explicitly owned Linux fixture project with isolated PostgreSQL/Redis.
   Record revision, image identity, queue subscriptions and resource limits without secrets.
2. Through normal public admission, start a synthetic application operation whose
   fixture holds it at a pre-submission browser boundary. Observe RUNNING and the
   held boundary before admitting discovery; keep external submission disabled.
3. Admit source discovery through the normal API/outbox path. Require its actual
   source consumer to complete before the browser gate is released, within a
   separately agreed bounded latency. Reopen PostgreSQL and read the operation,
   imported jobs and provenance; verify there was no submission request.
4. Negative controls: application consumer alone must not consume discovery;
   stopping the source consumer leaves discovery queued rather than falsely
   complete; saturated application capacity must not exhaust source capacity.
   Redis interruption preserves durable intent and recovery must not duplicate
   imported jobs. Do not infer browser release from lease expiry alone.
5. Repeat with an older normal-priority application backlog. Measure outbox wait
   separately from source execution so transport delay is not hidden by a healthy
   worker heartbeat. Release only request-owned gates/processes; preserve unrelated work.

## Concrete integration risk: shared outbox sweep

[workers/dispatch.py](../../workers/dispatch.py), `_pending_operations` and
`dispatch_pending`, selects all due QUEUED operation kinds, then performs serial
Redis job lookups/enqueues while holding one PostgreSQL advisory transaction lock
(`649106602`). Every worker runner starts the same all-queue dispatch loop.

Consequently, logical queue separation does **not** provide bounded dispatch
latency: older normal-priority application rows can precede normal-priority
discovery rows, and aggregate Redis work grows with that backlog. Per-call Redis
timeouts do not impose a sweep deadline. This is a source-visible coupling and a
testable risk, **not a measured production outage** or proof that an executing
browser holds the source worker's execution slot. Shared database, Redis and host
resources remain common failure domains even with separate consumers.

### Bounded proposal and ownership escalation

First reserve a new `tests/integration/test_queue_dispatch_isolation.py` and an
isolated runtime slot for the scenario above. That file is **not owned or edited**
by this documentation task. Preserve existing routing/priority tests as controls.

Only if that oracle demonstrates harmful cross-queue dispatch delay, consider a
separate implementation slice owning `workers/dispatch.py`, `workers/main.py`,
and focused queue-binding/dispatch tests. A candidate design is queue-scoped
selection with stable per-queue dispatch locks, preserving operation IDs,
within-queue priority, duplicate suppression and legacy all-queue callers.
Global expired-operation recovery must be audited explicitly; merely sharding
the Redis loop must not be advertised as complete failure isolation.

No new services, migration, queue renaming, acceptance-matrix edits or provider
submission is justified by this review. Primary integration ownership must grant
any additional paths before implementation. Issue #13 remains **OPEN / PARTIAL**
pending connected evidence and canonical adjudication outside this document's scope.
