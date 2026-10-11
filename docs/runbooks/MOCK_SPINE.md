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

Run the browser proof against a disposable fixture project with a unique
`mjobs-fixture-` project name and an unused loopback port. The browser proof
writes the baseline consumed by recovery; it contains the confirmed mock
application, completed operation, and stable caller `request_id`.

```powershell
$fixtureSuffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
$project = "mjobs-fixture-$fixtureSuffix"
$port = 18741
$composeFiles = @((Resolve-Path compose.yml).Path)
# For an approved workspace-local runtime override, append its resolved path here.
# $composeFiles += (Resolve-Path outputs/rc-2026-11/runtime.override.yml).Path
$env:COMPOSE_FILE = [string]::Join([IO.Path]::PathSeparator, $composeFiles)
$env:MJOBS_PORT = "$port"
$env:COMPOSE_PROJECT_NAME = $project
docker compose --project-directory . --profile fixture --project-name $project up -d --build
.venv\Scripts\python -m cli.ui_e2e --base-url "http://127.0.0.1:$port" --evidence outputs/ui-e2e.json
.venv\Scripts\python -m cli.compose_recovery --baseline outputs/ui-e2e.json --evidence outputs/recovery.json --project-name $project --port $port
```

The recovery command requires explicit project and port arguments and checks
every target container's Compose project, service, Compose file, working
directory, uniqueness, and running state before it changes service state. It
accepts a workspace-local runtime override through `COMPOSE_FILE` when the
root `compose.yml` is first and each override follows it, separated by the
platform path-list separator (semicolon in PowerShell). The same list must be
set when starting the project and running recovery; its exact comma-separated
Compose label is recorded in recovery evidence. Outside-workspace, duplicate,
missing, directory, symlink, and junction entries are rejected before mutation. It
then stops only this fixture's Redis and workers, persists a discovery request
while disconnected, starts the worker without dependencies, restores Redis,
and restarts the fixture services. After restart it reads the same explicit
mock-run request before replaying it, and verifies that the original operation
and independent mock receipt remain completed with submission count one. An
unconfirmed application or missing/mismatched baseline stops before replay.
External submission remains disabled. Do not run two recovery proofs
concurrently.

`docker compose --profile fixture stop` stops the project. `docker compose --profile fixture down` removes project
containers/networks while preserving named volumes. Do not remove volumes when
preserving data. Alembic migrations run before API/worker startup; use new
revisions instead of editing an applied revision.

## Current limits and recovery

- Discovery, document preparation, and browser application work have separate
  queues and worker processes. General cancellation and RUNNING leases remain
  incomplete.
- An interrupted pre-submit FILLING operation needs operator review. A
  SUBMITTING application is reconciled from a receipt or remains
  SUBMISSION_UNCONFIRMED. Never reset it to retry a click.
- CAPTCHA enters USER_INTERVENTION_REQUIRED. Manual resume is not implemented.
- Scoring currently covers required skills. Documents preserve approved facts
  and hashes; advanced tailoring, imports and answer matching remain pending.
- Each durable operation has a stable identity. Existing terminal operations
  are returned with their current state; no automatic failed-operation retry
  or recurring source scheduler is provided yet.
- Configured public Greenhouse boards are the only real read-only provider;
  real application providers, AI, and notifications remain incomplete.
- The active CI definition is `.github/workflows/ci.yml`. Workflow authorization
  was restored on 2026-09-30; the merged connected UI spine passed remote CI
  (see `docs/evidence/2026-09-30-ci-370.json`). The current PR head must pass
  its own workflow before any newer change is called verified.

For local tests run `python -m unittest discover -s tests -p test_*.py -v` using
the project environment. SQLite is only the explicit test backend; deployment
uses PostgreSQL and Redis. Secrets, documents and browser profiles are ignored.
