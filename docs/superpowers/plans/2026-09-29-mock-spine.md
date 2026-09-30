# Mock product spine implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Run the required fictional employer discovery through confirmed mock application and restart persistence.

**Architecture:** FastAPI modular monolith, relational durable authority and Redis/RQ dispatch; bounded HTTP and headless browser adapters over separate local mock services.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Redis, RQ, Playwright, Docker Compose.

**Spec:** docs/decisions/0002-first-vertical-spine.md; docs/spec/areas/test_harness.md MJ-REQ-208 through 213; linked scoped profile/application/persistence requirements.

## Global constraints

- No real external submission or fabricated career facts.
- No CAPTCHA/MFA/access-control bypass.
- Persist state before side effects; uncertainty stops submission retry.
- All dependencies are project-local; one browser worker and CPU-only execution.
- Preserve all original requirements and main history.

## Review focus

- Duplicate aggregator/employer records preserve provenance and share one canonical route.
- Missing or unapproved facts cannot appear in generated resumes/cover letters.
- A queue failure leaves a durable operation recoverable after restart.
- A crash after the final click cannot cause a second submission.
- Untrusted fixture redirects cannot reach external browser or HTTP origins.

## Task 1: Profile, discovery and durable data

Files: packages/persistence/models.py, services/discovery/pipeline.py, services/documents/generate.py, fixtures/mock_server.py, tests/integration/test_spine.py.

Interfaces: Store(url); discover(store, mock_origin, profile_id); prepare(store, job_id, profile_id, output_dir).

- [x] Write real local HTTP discovery tests: aggregator A, company careers link, ATS tenant, employer-only B; assert two canonical jobs and preserved postings after repeat collection.
- [x] Test document provenance and missing facts; run red before implementing.
- [x] Implement normalized route identity, factual matching/documents, application uniqueness and restart persistence.
- [x] Run local suite against explicit SQLite test backend, without claiming PostgreSQL evidence.

## Task 2: Controlled mock browser application

Files: services/applications/mock_adapter.py, workers/tasks.py, tests/browser/test_mock_application.py.

Interface: apply_mock(store, application_id, mock_origin, output_dir, submit=False) returns persisted status.

- [x] Write tests for two-page fill/upload/validation, dry-run stop, explicit mock-only submission/receipt, intervention and restart idempotency.
- [x] Run red, implement bounded requests and persisted SUBMITTING/confirmation state, then green.
- [x] Verify no external network origin is permitted and ambiguous outcomes never retry blindly.

## Task 3: Runnable API, durable queue and deployment

Files: apps/api/main.py, workers/dispatch.py, migrations/, compose.yml, infra/docker/Dockerfile, cli/, tests/e2e/.

- [x] Test API profile/discovery/application/operation reads and queue-disconnect recovery.
- [x] Implement API/worker and initial migration; add Compose PostgreSQL, Redis, mock service, API and a single worker.
- [x] Run build, migrations, health, complete mock E2E, restart and persisted readback.
- [x] Record local evidence, update state/queue, push real implementation, and open a stacked draft PR. Remote CI remains BLOCKED_EXTERNAL until workflow authorization is supplied.
