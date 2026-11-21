# MJ-REQ-160 — worker connection lifecycle cleanup

Run: `web-20261105T000133Z-e573e693-c10a-4b98-b2c7-698c3237ff2c`
Issue: [#161](https://github.com/MulattoTech/MJobs/issues/161)
Atomic claim: `refs/heads/webpro-claims/MJ-REQ-160` at `91889577de47aa52149dfbba65078fdaa3916596` (HTTP 201 and exact read-back).
Baseline main: `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`
Tested source: `0228651780462385a16eab69fcf3d725fd6beffa`
Whole requirement: **PARTIAL**. No original or additive acceptance counts changed.

## Reproduced failure and change

The worker entrypoint started its dispatcher thread before the Redis client acquisition entered its cleanup boundary. A client-construction exception could therefore leave that dispatcher running and skip Store disposal. A Redis close exception could also replace the original worker error or turn an orderly shutdown into failure, while skipping Store disposal.

The fix places dispatcher startup and Redis acquisition inside the cleanup boundary, joins only a successfully started thread, and releases the Store even if an earlier cleanup action fails. A Redis cleanup error emits fixed warning text and preserves the worker outcome. Other cleanup exceptions remain visible. Existing queue selection, first-recovery logging order, retry ceilings, dispatch interval, and five-second join bound remain unchanged.

The six new tests use real bounded dispatcher threads and synthetic Store/Redis/RQ boundaries. They cover client-construction failure, thread-start failure, orderly shutdown with a Redis close error, preservation of a primary worker failure, an unexpected close exception, and successful shutdown. Their own cleanup stops any deliberately leaked baseline thread.

## Executed evidence

| Revision / execution | Result |
| --- | --- |
| Unmodified main with the new tests, system Python | Collection ERROR before test execution: incompatible globally installed pydantic / pydantic-core versions. No product result. |
| Unmodified main with repository-pinned pydantic, core, RQ and pytest in an owned environment | **5 FAIL, 1 PASS**, 2.12 seconds, exit 1. Original failure output retained. |
| First changed-source focused invocation | **INCOMPLETE**: stopped only its verified owned pytest process after more than 180 seconds without a terminal result. Cause was not established; this is not counted as PASS or a fixed product failure. |
| Bounded diagnostic run on the changed source | **38 PASS, 16 passing subtests**, 0 failures/skips, 10.36 seconds; runner exit 0. |
| Exact committed source `0228651780462385a16eab69fcf3d725fd6beffa` | **38 PASS, 16 passing subtests**, 0 failures/skips, 57.71 seconds; runner exit 0. |
| Catalogue, progress journal, dashboard freshness, diff and source-commit provenance | **PASS**. Canonical acceptance files were unchanged. |

The successful runs overlap and must not be added together as independent coverage. An initial evidence-file staging check flagged Markdown hard-break whitespace; those trailing spaces were removed before publication.

Exact committed-source command, from the isolated repository root with its owned Python environment:

```text
python -u -m pytest -vv -p no:cacheprovider -o faulthandler_timeout=30 tests/unit/test_worker_connection_lifecycle_160.py tests/unit/test_worker_queue_binding.py tests/unit/test_worker_startup_logging.py tests/unit/test_configuration_contract_sustain_276.py tests/unit/test_extension_point_contracts.py tests/unit/test_dispatch_connection_recovery_160.py tests/unit/test_retry_matrix_sustain_161.py --tb=short --junitxml=outputs/web-e573e693-160/source.xml
```

The diagnostic and exact-source runners imposed a 150-second process deadline. Native runtime: Windows, Python 3.12.10. The owned virtual environment overlays repository-pinned pydantic 2.13.5, pydantic-core 2.46.5, RQ 2.12.0, and pytest 9.0.3; other dependencies are inherited read-only. No global package changes were made. Source receipt recorded at 2026-11-05T00:14:50.675669+00:00.

Private, reproducible logs in the owned worktree: `outputs/web-e573e693-160/baseline.log`, `baseline-pinned.log`, `baseline-pinned.xml`, `focused-interruption.txt`, `diagnostic.log`, `diagnostic.xml`, `source.log`, `source.xml`, and `source-result.json`. Public evidence contains only synthetic data and summary facts.

## Scope, limits, and delivery

This is a worker connection-lifecycle slice under the queue-disconnect clause. It adds no automatic application or paid-AI retry. It does not establish all MJ-REQ-160 criteria, real Redis/network outage acceptance, physical host restart, or the broader application/AI/browser retry policy.

The existing five-second join remains cooperative: this change does not forcibly interrupt an arbitrary blocked dispatch callback. Earlier configuration/registry/logger/thread-constructor failures outside the acquired-thread boundary are not covered. The initial incomplete local invocation is retained as an execution limitation, not silently reclassified.

Author source review and exact-source native checks are complete. Independent review, required hosted CI, actual refreshed merge candidate, protected merge and production deployment are separate facts and were **PENDING / NOT RUN** when this evidence was written. Consult the containing PR for subsequent exact-revision receipts.

No shared checkout, production process, live database, Cloudflare settings, external provider, application submission, Fleet session or quota control was changed. Rollback is a protected revert of the two source/test changes; no migration is required.
