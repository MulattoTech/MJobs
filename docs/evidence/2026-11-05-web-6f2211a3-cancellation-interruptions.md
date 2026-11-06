# PR944 follow-up: resolve cancellation outcomes on process-control exits

- Run: `web-20261105T024504Z-6f2211a3c35e48e1b993af0b62a7969a`.
- Scope: #770 and MJ-REQ-004; parent remains PARTIAL.
- Tested source: `5518b9f72073e38776b920c6ae79829e19e5735d`.
- Previous PR head: `5d9bde70b9bb53e391bc5ff1ce8464b61e8f8e0f`.
- Status: VERIFIED IN TEST; current-main refresh, fresh hosted gates,
  independent follow-up review, protected merge and release remain separate.

## Reproduced defect and correction

`request_cancel` resolved failed local cancellation attempts only when an
`Exception` escaped the transaction. `KeyboardInterrupt` and `SystemExit`
propagated after SQLite rollback without resolving the owned local outcome.
Watchdogs consequently retained a pending outcome until their finite wait
timed out. This was an additional interruption case beyond the earlier
ordinary-exception review; it does not invalidate those recorded passing tests.

The cleanup boundary now handles `BaseException`, resolves the local attempt
as uncommitted, then re-raises the original exception unchanged. A false local
outcome never authorizes cancellation: the existing durable-state read remains
authoritative. Successful commits, ordinary rollback/retry behavior, queued
and terminal guards, and application reconciliation are unchanged.

Two product/test files are touched: `workers/operations.py` and
`tests/unit/test_operation_cancellation_commit.py`. The real SQLite fixture
now checks immediate outcome resolution after transaction exit, exact
exception identity, unchanged cancellation state and audit rows after
rollback, watcher completion, and returned database connections.

## Executed verification

| Revision / phase | Executed result |
| --- | --- |
| Previous head, independent review | 15 tests and 20 subtests PASS; 0 failure/skip; 15.13 s pytest |
| Initial interruption baseline, unchanged product source | 2 failing interruption subcases, pytest exit 1 |
| Refined baseline with direct outcome oracle | 4 failing subcases across DISCOVER and AI_EVALUATE_QUEUED, pytest exit 1; 5.17 s |
| Committed source `5518b9f7` | 16 tests and 24 subtests PASS; 0 failure/skip; 14.16 s pytest / 15.015 s runner; exit 0 |
| Catalogue, journal validation, dashboard freshness, commit provenance, whitespace | PASS |
| Attempted `tools.verification.public_repo_policy` | NOT RUN: module is not present; invocation exit 1 retained in local receipt |
| Correct repository CI command `python -m tools.ci.check_repository` | PASS, 0 errors; resolves the invocation issue above |
| Hosted current-candidate gates and independent follow-up review | Pending; older-head success does not certify this change |

The baseline subtest summaries also report one completed parent test method;
that parent count does not turn the failed subcases into a pass. The initial
watcher assertion was refined to directly check unresolved outcome state,
avoiding an inference that the database transaction itself was still open.

Committed focused command, with output paths scoped to the owned worktree:

```text
python -m pytest -q -p no:cacheprovider --basetemp <owned-temp> --junitxml <owned-log> tests/unit/test_operation_cancellation_commit.py tests/unit/test_operation_control_reads.py tests/integration/test_source_adapter_connect_cancellation.py::SourceAdapterConnectCancellationTests::test_remoteok_public_cancel_during_real_tcp_connect tests/integration/test_durable_ai_evaluation.py::DurableAIEvaluationTests::test_cancel_closes_actual_controlled_openai_transport --tb=short
```

Execution used the existing project Python environment read-only, an isolated
worktree, synthetic SQLite stores and loopback HTTP fixtures. Provider/database
environment variables were removed from test children. No paid model endpoint
or employer service was called. Receipts, JUnit and red/green logs are retained
under the run-owned `outputs/web-6f2211a3/` directory.

## Ownership, rollout and limits

Atomic task ref `refs/heads/webpro-claims/PR-944-INTEGRATION` was acquired
with HTTP 201 and exact owner readback:
`962db26c2cb8565580b798ec5646ebdcc42ea1cf`. The scoped local branch is
`web/web-20261105T024504Z-6f2211a3/pr944`. Ownership was revalidated before
source edits. Four integration acquisition attempts returned HTTP 422 with
different owners; none granted ownership or performed a merge.

This is process-local cleanup evidence, not proof of forced-process-kill,
physical-host restart, all cancellation latencies, every historical timeout
or full MJ-REQ-004 acceptance. The new follow-up is author-reviewed only at
this checkpoint; prior separate-session reviews apply to their older sources.
No schema, retry admission, deployment, quota, canonical acceptance matrix
or GitHub Project state changed. Rollback is a protected revert of this scoped
source commit after ordinary current-main checks.

Read-only installed-service observations at 2026-11-05 02:58 UTC found
`/__deployment` reporting `0d3fbc5749fba8c682033a343a7ec9695e4dd743`
in PERSONAL mode and `/ready` reporting all database/queue/worker/scheduler
and schema checks true. Unauthenticated public root and readiness requests
redirected to Cloudflare Access; the tunnel requires Access validation.
Authenticated Brave verification was unavailable because the Browser tool
reported missing originating browser context. GitHub Project readback failed
for missing `read:project`; no permission scope was changed. These observations
do not certify rollout of this PR or the external-evaluation access policy.
