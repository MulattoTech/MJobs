# Codex lane handoff

Status: **ACTIVE / issue #6 / MJ-REQ-005**

## Completed issue #390 / MJ-REF-020

Integrated on main `300cf704915574d4e33beb251af14b003bd384d9`; issue closed with sanitized receipt at https://github.com/MulattoTech/MJobs/issues/390#issuecomment-6036388938. Evidence: `docs/evidence/2026-10-07-external-application-capture.json`.

## Active issue #6 / MJ-REQ-005

Branch: `codex/mj-req005-postgres-concurrency-20261007`
Worktree: `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req005-postgres-concurrency-20261007`
Base: `300cf704915574d4e33beb251af14b003bd384d9`
Candidate: `9e282135e9c90d56301b9d8726479479d84a862a` (tree `fd7665952a141c07a494f3a20b9986766363085b`)
Evidence target: `docs/evidence/2026-10-07-postgres-concurrency-runtime.json`

The prior skipped PostgreSQL category-admission proof now ran against an ephemeral, fresh local PostgreSQL 17 container. Added a cross-kind `APPLY_MOCK` capacity-one race across independent Store engines, with durable `RUNNING`/`QUEUED` state and attempts readback. Both company-discovery and application-automation PostgreSQL race tests plus operation-category unit and worker lifecycle tests passed: 21 tests, repeated twice on the same isolated DB. The first re-run against a reused test database correctly exposed old RUNNING test rows; regression cleanup now marks only its unique test-owned rows CANCELLED in `addCleanup`, and repeated execution passes. The existing personal MJobs PostgreSQL container was never used or restarted.

This is a verified slice, not full MJ-REQ-005 completion: no real ATS provider/browser submission was exercised; external submission remains OFF. Hosted checks remain NOT_RUN_BY_USER_POLICY.

Other active lanes: Antigravity #302 owns `services/discovery/sources/**` and Dice tests; Ollama/OpenCode #318 owns `services/ats_detection/**` and detector tests. Avoid both. Next, finalize the evidence/lane checkpoint, fetch current main, reconcile all claims, acquire integration lock, update canonical acceptance records without shrinking 366 requirements, integrate if still a safe fast-forward, release claims/lock, and continue.


## Active issue #96 checkpoint — 2026-10-07T12:18:49Z

Project and accomplishment rewrite slice is committed/pushed at source `97905378cdc79e437a7cae2fe2fbb7c504d0affe` from base `911f248db3110f095c9da98a452bab8797aeda16` in isolated worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req095-reviewed-achievement-rewrites-20261007`. Red test reproduced unsupported fact kinds. Unit, integration and connected Chromium acceptance passed; full Python/browser suite passed 908 tests with 5 skips; frontend 63/build and local validators passed. Evidence `docs/evidence/2026-10-07-resume-reviewed-achievement-rewrites.json` is being recorded. Issue #96 remains OPEN/PARTIAL. External submission OFF; hosted checks/review NOT_RUN_BY_USER_POLICY.


## Issue #96 released — 2026-10-07T12:20:17Z

Integrated at main `cc9fc3832f47e45cb9e60615c56b430692c3c614` from source `97905378cdc79e437a7cae2fe2fbb7c504d0affe` with evidence `docs/evidence/2026-10-07-resume-reviewed-achievement-rewrites.json`. GitHub release readback https://github.com/MulattoTech/MJobs/issues/96#issuecomment-6037769866. User-reviewed project/accomplishment rewrites preserve source provenance/history; full Python/browser 908 PASS, 5 skipped; frontend 63/build PASS. Requirement remains OPEN/PARTIAL. Claim released; continue from fresh reconciliation.
