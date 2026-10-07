# Codex lane handoff

Status: **ACTIVE / issue #23 scoped acceptance locally verified; integration pending**

## Active issue #23 / MJ-REQ-022

Claim: `codex/mj-req022-search-profile-selection-20261007`, worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req022-search-profile-selection-20261007`.
Base main: `ece8b69189a5589a079c2b01aa43a57037605d07`; source commit: `74acc9f0b472034b575baa9f45d5aafe5ce7a4c0`.
Evidence: `docs/evidence/2026-10-07-search-profile-selection.json`.

The Jobs view now exposes saved searches belonging to the active career profile and sends only a currently owned selected ID to the existing `/jobs` filter. A loopback Chromium test verifies API parameters and results, clearing the filter, persistence after Store reopen, career-profile switching/isolation, and no external browser requests. The browser run uses temporary SQLite; no schema change was made.

Latest verification: focused search/scheduler/browser tests 18 PASS; frontend 63 PASS; TypeScript/Vite production build PASS; agent-skills, catalogue, progress, lane validators, and diff check PASS. A first rerun with global Python failed to import SQLAlchemy/Playwright; repeating with the checkout's declared dependency environment `C:/Tools/Dev/MJobs/.venv/Scripts/python.exe` passed all 18 tests. Node 25.9 emits the recorded Vitest engine notice, though tests/build pass. PostgreSQL is NOT_APPLICABLE for this no-schema-change slice. Hosted checks/review NOT_RUN by policy; external submission OFF.

Next: acquire the integration lock; fast-forward only if the candidate remains based on current main; update canonical requirement/capability/progress/queue/handoff state; verify local and remote hashes; post sanitized evidence and close #23 only after readback; release claim and lock. Then recheck claims and select the next unclaimed dependency-ready issue.

## Other active lane claims

- Antigravity #302 / MJ-REQ-017.S01: `services/discovery/sources/**`, `tests/integration/test_dice.py`.
- Ollama #318 / MJ-REQ-063.D01: `services/ats_detection/**` and detector tests.

The previous Codex issue #403 / MJ-REF-033 was integrated only as a PARTIAL slice at main `ece8b69189a5589a079c2b01aa43a57037605d07`; it remains open with browser collection integration, page budgets, bounded diagnostic slice reruns, and in-flight synchronous handler preemption unimplemented. Its evidence is `docs/evidence/2026-10-07-agent-run-budget-output-validation.json`.

Hosted Actions/reviews remain disabled; external submission remains OFF. No unavailable check is promoted to PASS.
