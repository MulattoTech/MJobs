# Monitoring control-read diagnostics — MJ-REQ-004

Run: `webpro-20261103T034424Z-23e5`. Original requirement **MJ-REQ-004 remains PARTIAL**. This is a test-only failure-observability contribution, not a demonstrated correction of the original hosted trigger.

## Retained hosted failure and unchanged-source investigation

[Integration run41596437400, shard2/job125785299216](https://github.com/MulattoTech/MJobs/actions/runs/41596437400/job/125785299216) failed on PR909 head `e2ddb0683f91e9a69e518b3183c6566836f29ef4`, actual candidate `2566b5ca59fe3a3fed294599eacebfd06cccde34`, tree `14f8e197def0cb894f8321a5484b22ff90bf6177`. Result: **1 failed,945 passed,9 skipped,561 subtests passed**,678.17s. The positive real TLS sampling test required COMPLETED but received BLOCKED / OPERATION_CONTROL_UNAVAILABLE. Its underlying DBAPI, pool or direct-guard exception was not retained. Artifact13213056818 has ZIP SHA256 `d863941c67f7cfaafd5b88513e3b939007611608c64d84ba166b7a3682851eae`.

The relevant monitoring test, fixture, operations/tasks, public client, policy and persistence implementation are unchanged from base `63cb1042e08375f7bda1f9dad378209cca856167`. Source/order review found no cold-TLS fixture contamination: that separate test runs later, patches only its local fixture scope and restores its private worker clock binding. This does not prove the cause of the hosted incident.

One initial unchanged-candidate attempt passed in12.28s. The predeclared additional diagnostic plan then ran exactly three fresh normal cases and one separate private-lock probe at08:06:06–08:06:50UTC. The normal cases passed in11.875/11.719/11.531s, each saving eight jobs, one import receipt and the monitoring receipt. They are retained **non-reproductions**, not clearance of the hosted failure or additional independent feature coverage.

## Deliberate negative probe

The fourth case injected a bounded exclusive lock into its own temporary SQLite database after a real owned TLS request became active. It did not touch production, another worker's database, journal mode, authorization logic, real clocks, operation deadlines, retries or external providers.

The real reader raised its original unavailable exception at line523 with SQLAlchemy/dbapi OperationalError and SQLite SQLITE_BUSY (5). The measured437ms is whole-reader elapsed time, including checkout and cleanup, not a SQL-only bound. The worker persisted BLOCKED with zero jobs/import receipts and no monitoring receipt. The original positive COMPLETED assertion failed as expected; the probe is labelled **EXPECTED_FAULT_INJECTION_REFUSAL**, not a product PASS.

Acquisition/refusal/release ordering, ordinary delete journal mode, rollback, connection closure, controller/server/thread joins and original fixture cleanup all passed. A later server SSLEOF was secondary in this induced case only. The exact hosted trigger remains **UNKNOWN / NOT_REPRODUCED**.

`diagnostic-results.json` is the exact sanitized receipt, SHA256 `6d6aad7bb3cdc4110c5c87e14cf7a87ce3a60d3ad618f8816f4ef6d4ad3add27`. `diagnostic-plan-source.py.txt` preserves the frozen executed helper, SHA256 `2783c07417bb775a1a4e74fc61f06776055b98042de9fdf249888e620ea926c5`; it is historical evidence, not a general runner. Its aggregate predicate checked status strings but omitted child exit codes. Root independently checked **all four child_exit_code values equal0** before accepting completion. This caveat is retained; the frozen helper was not changed during execution.

## Reviewed source contribution

Source commit `62489046e80ea5a3a230fefd7f38b464dee6ecad`, tree `1f68f93aa33af76e4bf37cce7f9d08e66b083776`, adds an observer only in `tests/integration/test_company_monitoring_policy_transport.py`. The test blob is `1f8885810ed4ce94de6c6740d08cebd6a1f02ad3`; Git LF SHA256 `fe7f1ed5fe3b029bebd78cb504d6554376f2adef64e41b2f8dedfbc58b84b058`.

The saved real reader remains authoritative. The callable returns its exact result or bare-raises the same unavailable exception. A failed existing COMPLETED assertion gains at most eight safe metadata records plus overflow. Known class labels, bounded integer SQLite codes, reader code-object line numbers and real elapsed timing are retained under a lock. Traceback traversal stops at64frames; no SQL, parameters, exception messages, paths, connection objects, raw traceback or environment is serialized. Metadata formatting failure uses a fixed fallback. Successful execution stays quiet.

Four small tests exercise delegation/result identity, original exception identity and privacy, bounded detached snapshots, pool-timeout distinction and formatting fallback. Independent static review found no must-fix issue. All twelve original class methods remain present; only the sampling helper changes. The other eleven are AST-identical, including the byte-identical incompatible-rates method owned by PR914. Its separate pending privacy assertion change must be preserved during later reconciliation; no raw validation response detail is restored by this contribution.

## Exact source validation

On Windows/Python3.12.10 at08:20:27–08:21:29UTC, source62489046 passed:

- The complete monitoring-policy transport module plus the two focused scanner-policy modules: **17 parent tests and7 subtests; zero failures, errors or skips**. Pytest subprocess wall time61.438s.
- Focused Ruff F checks, catalogue, progress validation, dashboard consistency, contribution provenance and whitespace checks.
- Exact source HEAD/tree, clean tracked/untracked state, expected three-file source scope, exact imported policy blobs and preservation of unrelated original methods.

`source-validation.json` records commands and outcomes. `tests.xml` removes only the hostname attribute and normalizes UTF-8/LF; its published SHA256 is `d613ffec129b97783561ddadc496577baff9877a7574130ade6c78e201edc6bb`. The original JUnit is retained privately with SHA256 `6759e7ef2678cbb5a43f0f9e4f9a78382879dd45ac6f07b46679adb30920b4ef`.

These source results are distinct from future PR candidate/hosted checks. A future green instrumented run must not be described as a demonstrated repair of the original unknown trigger.

## Coordinated scanner input and remaining gates

The exact two-file scanner correction from source owner run1e2ff1's `e2ddb0683f91e9a69e518b3183c6566836f29ef4` is reused under [PR909 comment6598421758](https://github.com/MulattoTech/MJobs/pull/909#issuecomment-6598421758). It narrowly dispositions the independently verified historical Git-object metadata fingerprint. The ignore-list blob is `dd56b9f6b5aad00df72c4608b1a450cacc9e6fe2`; its proof blob is `bb6571a6ff44f9622e31e5a51176a377ee641542`. No additional exceptions or independent policy implementation credit are claimed.

No production, dependency, workflow, schema, acceptance matrix, journal or canonical handoff file changes are included. This evidence does not grant merge or deployment authority, change acceptance counts, clear PR909, or close issue5. Exact candidate verification and all protected hosted checks remain required. The source/failure decisions are recorded in [PR909 comment6598659724](https://github.com/MulattoTech/MJobs/pull/909#issuecomment-6598659724); the shared-file coordination is [PR914 comment6598660032](https://github.com/MulattoTech/MJobs/pull/914#issuecomment-6598660032).
