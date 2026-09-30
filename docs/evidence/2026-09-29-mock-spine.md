# Mock spine verification

Result: **PARTIAL** for the full product. Local controlled mock behavior is
verified; broader requirements and remote CI are still pending. All test career
data is fictional. No real external application was submitted.

| Target | Action | Result | Evidence |
|---|---|---|---|
| Local suite | Python 3.12 unittest discovery, 47 tests | PASS | `2026-09-29-spine-final-suite.log` |
| PostgreSQL schema | Compose Alembic upgrade, revisions 92f0997f753e and 098f12099319 | PASS | `2026-09-29-compose-final-up.log` |
| Full mock application | `python -m cli.mock_e2e`, UTC 2026-09-30T00:23:52Z | PASS | `2026-09-29-compose-final-e2e.json` |
| Outage/restart | `python -m cli.compose_recovery`, UTC 2026-09-30T00:24:05Z | PASS | `2026-09-29-compose-final-recovery.json` |
| Remote CI | GitHub push of workflow refused for missing workflow OAuth scope | BLOCKED_EXTERNAL | `2026-09-29-workflow-scope-blocker.md` |

The canonical Docker proof used manifest
`sha256:6b80034c133e8cb39a8d9d511e1ab10f03d2c90952979cbeba26ab685752b2ba`.
Earlier red/green and build logs are diagnostic history. The two historical
overlapping recovery invocations are not canonical evidence; the final recovery
ran alone against the final image after correcting Compose's dependency startup
with `--no-deps`.

The preserved original specification's SHA256 is
`1cf94181ae4b8e599063ada219759cb39b766f9d1bb7d860b99f4bb58adc0998`.
The existing GitHub main remains at
`4b8b6ed8e3b3e792d78e55df4742a7cd3b05244c`.

## Acceptance and limitations

The tests execute a real HTTP company flywheel: syndicated board Job A resolves
through employer careers and ATS redirects, enrolling one company source,
preserving four postings and two canonical jobs, including employer-only B.
Scores are scoped to the selected immutable profile version. Preparation
serializes concurrent requests and checks that four generated files match their
stored provenance/hashes. The real two-page browser flow uploads PDFs, fills
approved fixture answers, validates, and either stops before Submit or confirms
one explicitly enabled mock submission. CAPTCHA stops for intervention;
unavailable confirmation persists uncertainty with no blind retry. Counters use
submission time rather than preparation time.

The durable outbox survives Redis disconnection. Worker supervision reconnects
after Redis returns. Restarting PostgreSQL, Redis, mock ATS, API and worker keeps
the exact confirmed application, operation and independent receipt. Repeated
operation identity returns its terminal state without another submission.

MJ-REQ-212's controlled company-discovery behavior is demonstrated. MJ-REQ-213
remains PARTIAL because its required CI-level execution is blocked. Other linked
requirements receive only scoped evidence, not blanket completion. One-page and
conditional forms, unknown-company cases, real providers, recurring scheduling,
manual intervention resume, general cancellation/leases, independent queues,
frontend, import, AI and the broader matching/document specifications remain
pending. No umbrella issues are auto-closed by this PR.

## Review rulings

Independent read-only reviews found default mock DNS admission, receipt network
errors, profile score mixing, preparation file races, queue readiness and
dispatcher concurrency defects. These were fixed and relevant tests added.
Dispatch now holds a PostgreSQL advisory transaction lock through Redis enqueue;
database state claims prevent duplicate task execution. The dispatch thread logs
only error categories and retries unexpected operational failures.

The proposed stale-caller state-clobber finding was rejected after tracing:
the FILLING claim occurs before the error handler, so a failed claim cannot run
the owner's recovery transition. Broader RUNNING leases and manual challenge
resume are recorded pending work. The reviewer initially used system Python
without project dependencies; authoritative tests used the project `.venv`.
