# Worker lifecycle evidence

Scoped result: PASS for executed lifecycle contracts and deployed mock flow.
Full MJ-REQ-004/005 coverage remains PARTIAL; AI/OCR and separate company limits
are outside the implemented path. No real external application was submitted.

- `python -m unittest discover -s tests -p test_*.py -v`: 55 tests PASS, including
  real headless cancellation before submission reservation, expired-lease review,
  receipt uncertainty becoming BLOCKED, capacity and bounded retry contracts.
  Evidence: `2026-09-30-worker-full-suite.log`.
- Alembic revision `547acf656c37` adds attempts, cancellation and lease expiry;
  the Boolean default is portable across PostgreSQL and SQLite. Existing applied
  revisions were preserved.
- Docker build and three logical queues/processes (discovery, documents,
  applications) ran successfully. Evidence: `2026-09-30-worker-docker-build.log`
  and `2026-09-30-worker-compose-up.log`.
- `python -m cli.mock_e2e`: PASS at 2026-09-30T11:33:23Z, exact application and
  operation retained in `2026-09-30-worker-compose-e2e.json`. Document preparation
  is now durable asynchronous work. Submission count is one.
- `python -m cli.compose_recovery`: PASS at 2026-09-30T11:33:41Z, Redis outage and
  all-service restart retain the exact receipt and resume pending discovery.
  Evidence: `2026-09-30-worker-compose-recovery.json`.

The migration, task executor and Compose API were tested together. Direct
SIGKILL of an in-flight browser and full manual intervention resume remain
additional validation/implementation work; this report does not imply they ran.
Browser crash recovery conservatively stops pre-submit state for review, and
post-reservation recovery only reconciles a receipt. A terminal blocked result
does not become successful merely because the adapter returned normally.

Independent review identified missing cancellation enforcement and dishonest
uncertain completion in the unfinished executor. Both were fixed and exercised.
Project-local test temporary storage was used because the managed Windows
environment prevented SQLite from opening the default external temp directory.
No permissions or host settings were weakened.
