# Codex lane handoff

Status: **ACTIVE / issue #390 / MJ-REF-020**

## Active issue #390

Branch: `codex/mj-ref020-external-application-capture-20261007`
Worktree: `C:/Tools/Dev/MJobs/.worktrees/codex-mj-ref020-external-application-capture-20261007`
Base main: `ab49c58e401ec93c2cce8614c499242e414e9e31`
Evidence target: `docs/evidence/2026-10-07-external-application-capture.json`

Issue #390 adds local UI/API capture of user-reported applications completed outside MJobs. The service reconciles canonical application URLs before cautious exact company/title matching, creates a minimal provenance-marked job when required, records an append-only source/time report, and reserves profile-scoped idempotency keys before creation. SAVED/READY can advance to APPLIED; later stages remain unchanged. Existing prepared application classification is preserved. No employer traffic, provider call, or worker task is made.

Focused integration: 10 PASS. Connected browser: 6 PASS. Frontend: 63 PASS and build PASS. Full repository Python suite: 895 PASS, one optional PostgreSQL skip. Repository validators PASS. Source commit: `63142751719eb7b52f5fafd2e93dc47a6f53aa51`; tree: `f70b3ef5884b55525e5faa737f1d962510527900`. Evidence: `docs/evidence/2026-10-07-external-application-capture.json`. Live PostgreSQL and hosted checks are NOT_RUN; external submission stays OFF.

Reference provenance was updated clean-room for Personal JobPilot at pinned commit `a582c29d0f1aed97b93b13d843671487ab8c25d3`; no third-party code was incorporated.

## Other active lane ownership

- Antigravity #302: `services/discovery/sources/**`, `tests/integration/test_dice.py`.
- Ollama/OpenCode #318: `services/ats_detection/**`, detector tests.

Local acceptance is complete. Next fetch current main, recheck claims/worktrees, acquire the integration lock, reconcile canonical artifacts, fast-forward/push only if current main is an ancestor, verify SHAs, and close #390 only for fully evidenced acceptance. Release claim and lock; immediately select and claim the next highest-value unclaimed dependency-ready issue.
