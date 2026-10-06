# Codex lane handoff

Status: **ACTIVE**

Issue #111 / MJ-REQ-110 profile-scoped cost controls and deterministic preview are
integrated in canonical main `54568c7de5edc3594f39f9d139f449bb0eed8cae`.

The active Codex slice is the remaining review-gated production task-consumer
acceptance inside active MJ-PR-030. Do not redo the integrated settings/preview.
The dedicated worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req110-review-consumer-20261006`
is on `codex/mj-req110-review-consumer-20261006`, fast-forwarded to current
origin/main `54568c7de5edc3594f39f9d139f449bb0eed8cae`. Antigravity owns #303
source-adapter paths; Ollama owns #318 ATS-detection paths.

Checkpoint: `services/ai/consumer.py` and the profile-scoped evaluate API route
implement bounded deterministic candidate admission and structured AI job scoring.
Job descriptions are passed as bounded untrusted text; model results are validated;
deterministic match scores remain unchanged; this route creates zero documents.
On this resume, all 9 focused AI unit/API tests and agent-skill, catalogue, progress,
compile, and diff validators pass. The prior full Python run recorded 785 tests with
2 failures, 6 errors, and 15 skips in browser tests. Frontend checks remain unrun
because the prior checkpoint found `vitest` and `tsc` absent. Live inference and a
reviewed AI-document workflow are not implemented or verified. Hosted checks remain
NOT_RUN by policy. Current evidence: `docs/evidence/2026-10-06-ai-cost-consumer-resume.json`;
preserved earlier record: `docs/evidence/2026-10-06-ai-cost-consumer.json`. Keep
issue OPEN/PARTIAL until the remaining paths receive their own authorized evidence.

During implementation write only Codex lane files plus owned feature/evidence paths.
Canonical planning and root main require the integration lock.

Current-main reconciliation commit: `600156158227997bedbe8dcc9e03d40d625e4557`.
