# Controlled mock product spine

This milestone is a runnable local backend, durable worker and fictional ATS.
It does not yet provide the complete job platform or a web dashboard. Real
external submission is disabled, and this adapter only accepts the configured
local mock origin.

## Start and verify

Use Python 3.12 and Docker Compose from the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python -m cli.init_env
$env:MJOBS_OPERATION_MODE = 'FIXTURE'
$env:MJOBS_COMPOSE_MOCK = '1'
docker compose --profile fixture up -d --build
.venv\Scripts\python -m cli.mock_e2e --evidence outputs/mock-e2e.json
```

The explicit fixture profile starts the fictional ATS. Plain `docker compose up`
uses PERSONAL mode and omits that service. The proof creates only an explicit fictional Alex Fixture profile. It discovers
the board's Job A, resolves Example Systems' careers page and mock ATS, enrolls
the employer, discovers employer-only Job B, matches verified skills, generates
PDF/DOCX resume and cover letter, queues a real headless browser form flow and
verifies a persisted mock receipt. Repeating the exact queued operation keeps its
identity and does not click Submit again.

The API is at `http://127.0.0.1:18700`, with OpenAPI at `/docs`. `/health` checks
database liveness; `/readiness` checks database, Redis, a worker for the `mjobs`
queue and the mock service. `/settings` identifies FIXTURE operation mode and external OFF.
Supply `profile_id` to `/jobs` to obtain that profile version's match scores.
Profile facts and approved answers must be explicit, sourced and verified.

## Persistence and outage proof

```powershell
.venv\Scripts\python -m cli.compose_recovery --baseline outputs/mock-e2e.json --evidence outputs/recovery.json
```

This explicitly stops only this project's Redis/worker, persists a discovery
request while disconnected, starts the worker without dependencies, restores
Redis, then restarts all five long-running services. It checks the original
application, operation and independent mock receipt and requires submission
count one. Do not run two recovery proofs concurrently.

`docker compose --profile fixture stop` stops the project. `docker compose --profile fixture down` removes project
containers/networks while preserving named volumes. Do not remove volumes when
preserving data. Alembic migrations run before API/worker startup; use new
revisions instead of editing an applied revision.

## Current limits and recovery

- The single worker shares discovery and browser work; independent queues,
  general cancellation and RUNNING leases are still required.
- An interrupted pre-submit FILLING operation needs operator review. A
  SUBMITTING application is reconciled from a receipt or remains
  SUBMISSION_UNCONFIRMED. Never reset it to retry a click.
- CAPTCHA enters USER_INTERVENTION_REQUIRED. Manual resume is not implemented.
- Scoring currently covers required skills. Documents preserve approved facts
  and hashes; advanced tailoring, imports and answer matching remain pending.
- Each durable operation has a stable identity. Existing terminal operations
  are returned with their current state; no automatic failed-operation retry
  or recurring source scheduler is provided yet.
- No real read-only providers, application providers, AI or notifications are
  included in this milestone.
- The active CI definition is `.github/workflows/ci.yml`. Workflow authorization
  was restored on 2026-09-30; the merged connected UI spine passed remote CI
  (see `docs/evidence/2026-09-30-ci-370.json`). The current PR head must pass
  its own workflow before any newer change is called verified.

For local tests run `python -m unittest discover -s tests -p test_*.py -v` using
the project environment. SQLite is only the explicit test backend; deployment
uses PostgreSQL and Redis. Secrets, documents and browser profiles are ignored.
