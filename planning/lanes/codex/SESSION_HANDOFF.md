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
The AI Settings UI now permits explicit evaluation after preview and displays
advisory results with job title/company context. Job descriptions remain bounded
untrusted text; model results are validated; deterministic match scores remain
unchanged; this flow creates zero documents and evaluation history is not durable.
This checkpoint ran `git diff --check` only. The affected API tests, frontend tests,
browser flow, and build are NOT_RUN. Live inference and a reviewed AI-document
workflow are not verified. Hosted checks remain NOT_RUN by policy. Current evidence:
`docs/evidence/2026-10-06-ai-cost-consumer-review-ui.json`; prior evidence remains in
`docs/evidence/2026-10-06-ai-cost-consumer-resume.json` and
`docs/evidence/2026-10-06-ai-cost-consumer.json`. Keep issue OPEN/PARTIAL until the
remaining paths receive their own authorized evidence.

During implementation write only Codex lane files plus owned feature/evidence paths.
Canonical planning and root main require the integration lock.

Current-main reconciliation commit: `600156158227997bedbe8dcc9e03d40d625e4557`.
