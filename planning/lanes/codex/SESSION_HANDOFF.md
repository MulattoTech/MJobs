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
