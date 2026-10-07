# Codex lane handoff

Status: **ACTIVE / issue #6 scoped acceptance locally verified; integration pending**

## Active issue #6 / MJ-REQ-005

Claim: `codex/mj-req005-application-category-capacity-20261007`, worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req005-application-category-capacity-20261007`.
Base main: `aa28501b0db8a00e9331d43347e2bbdfc3ed2b98`; source commit: `704b2fc3fadd00d4e40ca1bea4ccbbe3a0c3bf50`.
Evidence: `docs/evidence/2026-10-07-application-automation-category-capacity.json` (evidence and lane-state checkpoint pending commit).

The new integration regression races two APPLY_MOCK claims through independent Store engines over the same SQLite database at a configured application-automation limit of one. It verifies one durable RUNNING claim, one still-QUEUED operation with no attempt, and independent PREPARE capacity. This adds missing acceptance coverage for the existing database-backed category lock; no product source change was needed for this slice.

Validation on this source: worker lifecycle, dead-letter and category-concurrency suites 23 PASS; isolated race regression 1 PASS; diff check PASS. No migration or frontend change. Browser test, frontend test/build, full repository suite, live PostgreSQL, and hosted checks/review were NOT_RUN for this test-only slice. External submission remains OFF.

Next: fetch current main and reconcile active claims/worktrees. Acquire the integration lock; verify the exact candidate/base; update canonical requirement/capability/progress/queue/handoff evidence while holding the lock; fast-forward only if main is an ancestor; push and verify local/remote hashes. Post sanitized evidence on issue #6, keep it OPEN/PARTIAL because live PostgreSQL and real ATS behavior remain unverified, release the claim and lock, then immediately select the next unclaimed dependency-ready issue.

## Other active lane claims

- Antigravity #302 / MJ-REQ-017.S01: `services/discovery/sources/**`, `tests/integration/test_dice.py`.
- Ollama #318 / MJ-REQ-063.D01: `services/ats_detection/**` and detector tests.

Issue #23 / MJ-REQ-022 is integrated at main `805e52bab0528e562578f51f5426285af0163657`, with scope PASS evidence at `docs/evidence/2026-10-07-search-profile-selection.json`. Issue #403 / MJ-REF-033 remains an integrated PARTIAL slice; browser collection integration, page budgets, bounded diagnostic slice reruns, and in-flight synchronous handler preemption remain open.

Hosted Actions/reviews remain disabled. No unavailable check is promoted to PASS.
