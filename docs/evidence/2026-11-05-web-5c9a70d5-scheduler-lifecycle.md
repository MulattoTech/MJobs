# Scheduler startup and Redis cleanup — 2026-11-05

Run: `web-20261105T012848Z-5c9a70d51d99`.
Requirement: `MJ-REQ-160`, issue #161. The whole requirement remains **PARTIAL**.
Base: `0d3fbc5749fba8c682033a343a7ec9695e4dd743`.
Source commit: `f2343494cf7fbc68cf746e8bc12ac934a876c933`.
Branch: `web/web-20261105T012848Z-5c9a70d51d99/scheduler-lifecycle`.
Atomic claim: `refs/heads/webpro-claims/DISCOVERY-SCHEDULER-LIFECYCLE-SAFETY`,
owner `2c6c0edab812e960a05637b216aa5f9581890352`; HTTP201 plus exact readback
at `2026-11-05T02:14:40.235932Z`. A complete 21-open-PR file inventory found
no overlap with the three scoped paths. PR943's separate worker entrypoint
fix is not modified or claimed as this run's implementation.

## Reproduced failure and correction

The scheduler created a Store before validating its numeric intervals. Workspace
binding and Redis construction also occurred before its cleanup boundary.
Failure at any of those stages left the acquired engine undisposed. During
shutdown, a Redis close failure skipped database cleanup and replaced the original
exit or interrupt. Synthetic fault injection reproduced those paths on unchanged
base-main product code.

The correction validates intervals before Store creation, protects workspace
binding and Redis construction with the same cleanup boundary as the loop, and
always attempts engine disposal even if Redis close fails. Expected Redis errors
during cleanup emit one fixed warning and preserve the scheduler's original
outcome. Unexpected non-Redis cleanup errors remain visible. Arbitrary engine
disposal failures are not suppressed or certified by this change.

Retry policy, tick and retention bounds, heartbeat semantics, source admission,
durable operations, external-submission controls and Redis socket timeouts remain
unchanged. There is no schema migration or rewrite of existing data.

## Executed verification

The new regression module was first run while `services/discovery/scheduler.py`
was byte-identical to base main:

```text
python -m pytest -q -p no:cacheprovider tests/unit/test_discovery_scheduler_lifecycle_160.py --tb=short
```

**Expected FAIL:** pytest reported 12 failed results and 2 passed parent methods,
exit 1, 1.13 seconds. The failures comprise eight subcases (six invalid interval
cases and two shutdown signals) and four test methods; these are not 12 distinct
test functions. The earlier failure remains evidence, not a passing result.

After the correction, the exact source/test blobs committed at `f2343494...` ran:

```text
MJOBS_ENABLE_LIVE_TESTS=0 PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests/unit/test_discovery_scheduler_lifecycle_160.py tests/unit/test_discovery_scheduler_health.py tests/unit/test_discovery_scheduler_logging.py tests/unit/test_transient_retention_scheduler.py tests/unit/test_saved_search_scheduler.py tests/unit/test_saved_search_scheduler_validation.py --tb=short
```

**PASS:** 28 tests and 37 subtests, zero failures/skips, 3.86 seconds, exit 0.
The six new methods cover invalid configuration without resource allocation,
binding/missing-configuration failures, real SQLite engine disposal, durable
marker preservation and successful reopen/tick after failed Redis construction,
KeyboardInterrupt/SystemExit preservation, privacy-safe cleanup logging and
database cleanup despite an unexpected Redis-close exception. Existing suites
also exercise successful/failed scheduler heartbeat publication, tick recovery,
saved-search admission/cadence and configured retention behavior.

Catalogue validation, progress-journal validation, dashboard consistency and
`git diff --check` passed. Strict commit provenance passed for both source files.
An initial unpublished commit used a date-only UTC trailer and failed provenance;
the trailer was corrected to a real UTC timestamp before publication, without
changing source/test blobs. Full local logs and JUnit receipts are under the
owned checkout's ignored `outputs/web-5c9a70d5-scheduler/` directory.

## Remaining gates and rollout

At this source-evidence checkpoint, native Windows execution, independent review,
fresh required hosted CI, protected merge and deployment are **NOT RUN / PENDING**.
Later PR receipts must name their exact revision. Local tests use synthetic Redis
objects and an isolated SQLite database; they do not certify live Redis outages,
PostgreSQL-specific cleanup, physical restart or production readiness.

No acceptance matrix, denominator, Project/Fleet status or issue closure changes.
Merge requires current-main candidate checks and short exclusive integration
ownership. Deployment requires separate ownership and the existing guarded
verified-main updater. Rollback is a protected source revert; no database
downgrade, live destructive restore or migration is introduced.
