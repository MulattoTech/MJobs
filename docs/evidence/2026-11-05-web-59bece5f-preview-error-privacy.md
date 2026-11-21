# MJ-REQ-172: private saved-resume preview error codes

Run: `web-20261105T024508Z-59bece5f637e`.
Requirement: [MJ-REQ-172 / issue 173](https://github.com/MulattoTech/MJobs/issues/173).
Base: `d6650e4bc9bfd784bd6e3a346c793c372f02518a`.
Source commit: `45572d88dbb90e23d7fd2fa7c1c1737bb4dce837`.

## Reproduced boundary and correction

The saved workspace preview route copied service exception `code` attributes
directly into its public response. Synthetic unknown strings, including a large
diagnostic string, were echoed. Unhashable or unserializable values could escape
the intended validation response as HTTP500. This is fault-injected defense of
an internal service boundary; no external exploit or live applicant-data exposure
is claimed.

Only the documented seven codes from the current preview/template call graph
are now returned. Four stale-context codes retain HTTP409; the three documented
validation codes retain HTTP422. Other values return the existing fixed
`RESUME_WORKSPACE_PREVIEW_INVALID` code with HTTP422. The unchanged missing,
unavailable and invalid-readback responses retain HTTP404/503/500 respectively.
Exception messages, causes and notes remain excluded.

Success rendering, snapshot binding, approved fact provenance, storage,
authentication and authorization interfaces are unchanged. Future public error
codes must be deliberately added to this route's allowlist.

## Exact executed verification

Source blob: `14449527918efb10cda18399a261b5d066f6af5d`.
Regression-test blob: `942c02eb7dba2ac3436826feb6094b40be318021`.
The committed source and tests exactly match the executed blobs.

The focused regression selectors were:

```text
python -B -m pytest -q -p no:cacheprovider --basetemp <isolated-temp> --junitxml <isolated-receipt> tests/integration/test_resume_workspace_preview_error_privacy.py tests/integration/test_resume_workspace_preview_api.py tests/unit/test_resume_workspace_preview.py tests/unit/test_resume_templates.py --tb=short
```

| Run | Result |
| --- | --- |
| Corrected fixture against unchanged base product in a separate detached worktree | Expected RED: 35 failed results, 5 parent passes, 19 passing subtests; 7.78s, exit1 |
| Corrected product, initial isolated environment | 22 tests +70 subtests PASS; 16.86s, exit0 |
| Exact source commit, repository-pinned affected runtime/test tooling | 22 tests +70 subtests PASS; 16.82s, exit0; zero failures/errors/skips |

The 35 baseline failures are 34 failed parameter subcases and one failed
durable-recovery method. Parent/subcase and repeated-run totals overlap and must
not be added as independent feature coverage. Final JUnit has 92 executed cases,
including the 70 subtests, not 92 independent methods.

Pinned affected packages were installed only inside this run's private virtual
environment: FastAPI 0.142.1, httpx 0.28.1, Pydantic 2.13.5,
pydantic_core 2.46.5, SQLAlchemy 2.1.1, Starlette 1.7.0, pytest 9.0.3 and Ruff 0.17.2.
No shared or global dependency was modified. Native Python was 3.12.

The new cases exercise unknown/oversized strings, malformed scalars/collections,
both handled service-exception families, all seven public codes, bounded output
and private messages/causes/notes. A real isolated SQLite fixture is reopened
after faults: profile/job data, workspace revisions and application/document/
operation counts are unchanged. A subsequent real preview returns its exact
normal service contract. Existing foreign-profile, stale-context, template,
layout and provenance tests also pass. Local fixture HTTP is loopback only.

## Preserved verification limitations and harness errors

The first regression fixture used the logical name `operations` with
`Store.count`, while the existing model uses a different physical table name.
That initial run mixed 34 reproduced API failures with one fixture error, and
the first corrected-product run had one remaining fixture failure. The test now
uses the actual model table. Both earlier logs are retained; the corrected
unchanged-source baseline was executed separately before the final passing
runs. No product failure or skipped check was relabeled as a pass.

A commit-preparation helper initially stripped the leading status-column space
from Git porcelain output and refused its own two-file scope check. It stopped
before staging or committing. Preserving those columns corrected the helper;
the successful commit's two source blobs were verified against test receipts.

Ruff: the new regression module PASS. The production module retains exactly
three baseline findings, unchanged by code/message comparison with the detached
base: UP035, I001 and BLE001. Thus whole-file Ruff is NOT clean; no new finding,
suppression, rule change or unrelated source formatting was introduced.

Additional PASS at the committed source: catalogue, progress validation,
dashboard freshness, source provenance and whitespace. Protected hosted
candidate checks, independent technical review, PostgreSQL-specific execution,
authenticated browser behavior and production rollout are NOT RUN/PENDING here.

## Ownership, rollout and acceptance

Atomic scope claim:
`refs/heads/webpro-claims/RESUME-WORKSPACE-PREVIEW-ERROR-PRIVACY` at
`b2093dfb869b4ecda5a52c6bedecbe570055e60a`, HTTP201 with matching readback
at 2026-11-05T02:59:45.7568856Z. Revalidated before edits and source commit.
Branch: `web/web-20261105T024508Z-59bece5f637e/resume-preview-errors`.
Open-PR file scopes and the separately claimed HTTP transport cleanup do not
overlap the changed router/new test. No heavy/browser slot was needed.

This evidence commit changes neither tested source blob. Current-main
integration must honor all protected checks and its exclusive short lock.
Deployment requires separate ownership and the existing verified-main updater.
Rollback is a protected source revert; there is no schema or data migration.

**MJ-REQ-172 remains PARTIAL.** No canonical criterion, denominator, acceptance
record, Project/Fleet state or external-application behavior was changed.
Author review is not independent review. This run used no separate AI agent,
Codex/Orchestrator worker, paid model or live user data.
