# Disposable local MJobs fixture lifecycle

**Use the guarded runner for new Docker-backed development checks.** This is
an opt-in tool, not a replacement for the production/personal Compose
deployments. It never restarts MJobs Micro, production tunnels, Sat-Tracker,
personal PostgreSQL/Redis, or previously created unlabelled fixtures.

## Right-size the work

- Pure unit/static tests: **0 containers**; use Python/Node on the host or CI.
- Database/Redis-only integration: **2 containers** or GitHub-hosted services.
- End-to-end queue/browser/backup validation: full multi-service fixture is
  required. The API, five queue workers, migration, scheduler, mock, Redis,
  and PostgreSQL cannot safely be collapsed below five containers without
  modifying worker isolation, dependencies, and acceptance criteria.

The disposable overlay caps all service CPU ceilings at a combined **3.65
logical CPUs** (conservative host budgeting rather than individual 2-CPU caps),
and changes their restart policies to **no**. The tool permits one concurrently
running fixture by default, generates unique project names and credentials,
and never operates on the mjobs or mjobs-personal project.

## Commands (repository root, Python 3.12+, Docker Desktop)

Read-only overview of *all* fixture projects, including old/unmanaged ones:

    python -m tools.dev_fixture_guard audit

Run a synthetic E2E check in a temporary project (choose --build when the
source image needs rebuilding):

    python -m tools.dev_fixture_guard run --build --ttl-minutes 60 --port 18741 -- python -m cli.mock_e2e --api http://127.0.0.1:18741 --evidence outputs/mock-ephemeral.json

The runner chooses a free loopback port, passes it as MJOBS_PORT, and starts
the fixture with Docker Compose up --wait. To drive browser validation, use
a command that reads MJOBS_PORT for its base URL.
A test is terminated at its TTL; **do not use this tool for persistent user data,
lengthy interactive developer sessions, or live application submissions.**

For explicitly limited parallel tests, --max-fixtures 2 (maximum 3).
The runner refuses to start new work above this budget, including legacy
running fixture projects.

## Interruption recovery

- Normal exit, nonzero test status, or keyboard interruption: finally
  attempts a project-scoped down --remove-orphans --volumes.
- A detached, fixed-expiration watchdog survives most abandoned shell/chat
  sessions and reaps only its labelled disposable project at TTL.
- After an OS reboot, or if the watchdog itself was killed, the next guarded
  run first reaps expired *tool-owned* projects.
- To preview and explicitly clear expired projects:

      python -m tools.dev_fixture_guard sweep
      python -m tools.dev_fixture_guard sweep --apply

Both cleanup routes require a valid 12-hex fixture project, matching random
ownership labels on **every** container, known service identities, and expected
Compose volume/network names. If ownership is ambiguous or Docker is
unavailable, cleanup fails closed and reports a manual review requirement.
It does **not** remove unrelated containers, images, shared volumes, build
cache, or old unlabeled mjobs-fixture-* stacks. Do not use global
Docker system prune --volumes on this workstation.

The tool-generated fixture volumes are disposable and deleted when
ownership is proven. Production and personal volumes are never in scope.

## Maintainer policy

New agent-created Compose fixture work should use this guard, or an equivalently
reviewed disposable runner with bounded lifespan, unique ownership,
non-restarting containers, resource budgets, and secure cleanup. Never execute
an unbounded Compose up -d for a temporary project and leave it running.
If a recovery test explicitly requires restart behavior, run that test through
its existing isolated and separately reviewed recovery workflow instead: this
overlay intentionally disables restarts and is not suitable for testing them.

Run:

    python -m unittest tests.unit.test_dev_fixture_guard -v
    docker compose -f compose.yml -f compose.dev-fixture.yml config --quiet

The second command requires MJOBS_DEV_GUARD_TOKEN and
MJOBS_DEV_GUARD_EXPIRES, plus the usual synthetic Compose interpolation
variables. It **renders configuration only** and does not start containers.
