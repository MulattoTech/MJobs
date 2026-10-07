# Codex lane handoff

Status: **ACTIVE / issue #5 scoped acceptance locally verified; integration pending**

## Active issue #5 / MJ-REQ-004

Claim: `codex/mj-req004-upload-compare-cross-instance-20261007`, worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req004-upload-compare-cross-instance-20261007`.
Base main: `805e52bab0528e562578f51f5426285af0163657`; source commit: `dff77688f25fde18e24201dc8f62e6f9324e542b`.
Evidence: `docs/evidence/2026-10-07-uploaded-comparison-cross-instance-cancel.json` (evidence and lane-state checkpoint pending commit).

Uploaded-original comparisons now reserve a durable worker operation keyed by a hash of profile/application/revision/document scope. A second API instance sharing the operation store can cancel the in-flight comparison. The synthetic SQLite integration regression verifies HTTP 499, durable CANCELLED readback, wrong-profile denial, wrong-document non-cancellation, no raw resume identifiers or parameters in the operation row, and unchanged profile/application history. The regression was observed failing before implementation.

Validation on this source: focused Python/API suite 46 PASS; full Python suite 886 PASS with one optional PostgreSQL skip; connected headless Chromium browser test PASS; frontend 63 tests and TypeScript/Vite build PASS; agent-skills, catalogue, progress, lane, compileall, and diff checks PASS. No migration was changed. PostgreSQL runtime was NOT_RUN because `MJOBS_TEST_POSTGRES_URL` was unset. Hosted checks/review are NOT_RUN by policy; external submission remains OFF.

Next: fetch current main and reconcile active claims. Acquire the integration lock; verify the exact candidate/base; update canonical requirement/capability/progress/queue/handoff evidence while holding the lock; fast-forward only if main is an ancestor; push and verify local/remote hashes. Post sanitized evidence on issue #5, keep it OPEN/PARTIAL because broader operation/provider/ATS requirements remain, release the claim and lock, then immediately select the next unclaimed dependency-ready issue.

## Other active lane claims

- Antigravity #302 / MJ-REQ-017.S01: `services/discovery/sources/**`, `tests/integration/test_dice.py`.
- Ollama #318 / MJ-REQ-063.D01: `services/ats_detection/**` and detector tests.

Issue #23 / MJ-REQ-022 is integrated at main `805e52bab0528e562578f51f5426285af0163657`, with scope PASS evidence at `docs/evidence/2026-10-07-search-profile-selection.json`. Issue #403 / MJ-REF-033 remains an integrated PARTIAL slice; browser collection integration, page budgets, bounded diagnostic slice reruns, and in-flight synchronous handler preemption remain open.

Hosted Actions/reviews remain disabled. No unavailable check is promoted to PASS.
