# Codex lane handoff

Status: **ACTIVE / PARTIAL — safely checkpointed**

Issue #111 / MJ-REQ-110 remains the only Codex claim. Continue in
`C:/Tools/Dev/MJobs/.worktrees/codex-mj-req110-review-consumer-20261006` on
`codex/mj-req110-review-consumer-20261006`.

Fetched current `origin/main` at `b187e2ed7d84d1a9e7f6f27a1e771d597e7cf11f` and merged it into the claimed branch
at `380311801205b435db93c6a1c9c2351e3ea8a3ad` without conflicts. The implementation checkpoint is
`d872d45610ab32c79563462cee3e75f60c546eb9`.

This slice persists bounded per-profile evaluation history with the profile
version, cost policy, and non-secret provider/model identifiers. Stale results
are rejected if profile facts change during inference. The API and browser reload
history per profile; deterministic matches remain unchanged and no documents are
created. Evidence: `docs/evidence/2026-10-06-ai-cost-evaluation-history.json`.

Local verification: 11 focused Python/API/browser tests PASS; 62 frontend tests
and TypeScript/Vite build PASS; SQLite migration upgrade, empty downgrade, and
populated-history downgrade protection PASS; agent-skills, catalogue, progress,
compileall, and diff checks PASS. Validation used synthetic profiles/jobs and
structured provider output; no live model was invoked.

Keep the issue PARTIAL. Live configured-provider inference, reviewed AI-document
generation, PostgreSQL migration verification, and full Python discovery remain
NOT_RUN. Hosted checks remain NOT_RUN under the active local-only policy;
external submission remains OFF.

Only Codex lane files and owned feature/evidence paths may be changed during this
claim. Antigravity owns source-adapter paths; Ollama owns ATS-detection paths.
Canonical planning and root main require the local integration lock.
