# Codex lane handoff

Status: **ACTIVE**

Issue #111 / MJ-REQ-110 profile-scoped cost controls and deterministic preview are
integrated in canonical main `8066f781678b099dcfeba5d9cdaa26d1b2dbc97a`.

The active Codex slice is the remaining review-gated production task-consumer
acceptance inside active MJ-PR-030. Do not redo the integrated settings/preview.
The dedicated worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req110-review-consumer-20261006`
is on `codex/mj-req110-review-consumer-20261006`, fast-forwarded to current
origin/main `8066f781678b099dcfeba5d9cdaa26d1b2dbc97a`. Antigravity owns #303
source-adapter paths; Ollama owns #318 ATS-detection paths.

Checkpoint: added `services/ai/consumer.py` and a profile-scoped evaluate API
route. The policy bounds inputs, score threshold, model candidates, and reviewed
document candidates. Job descriptions are passed as bounded untrusted text; model
results are strict structured output; deterministic match scores remain unchanged;
this route creates zero documents. Focused AI unit/API tests (9) and agent-skill,
catalogue, progress, compile, and diff validators pass. Full Python validation ran
785 tests with 2 failures, 6 errors, and 15 skips in browser tests. Frontend tests
and build could not start because this worktree lacks `vitest` and `tsc`. No real
provider inference or document generation ran; hosted checks remain NOT_RUN by
policy. Evidence: `docs/evidence/2026-10-06-ai-cost-consumer.json`. Keep issue
OPEN/PARTIAL until live inference and the reviewed-document workflow are addressed.
Implementation checkpoint commit: `5e1b9da`.

During implementation write only Codex lane files plus owned feature/evidence paths.
Canonical planning and root main require the integration lock.
