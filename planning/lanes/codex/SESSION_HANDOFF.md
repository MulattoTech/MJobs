# Codex lane handoff

Status: **ACTIVE / locally verified**

## Latest completion

Issue #381 / MJ-REF-011 is locally complete and integrated at `8bb20aeb08dbb696af0629bfc861833c7bce0220` from base `977178a837003136f45308ab2d18e2c046eabb3c`. A single monotonic budget bounds the full AI evaluation batch and each provider call receives only the remaining time. Job descriptions are explicitly truncated with omitted-character counts; the prompt forbids inference from omitted content. Timeout and cancellation outcomes persist as structured terminal operations and never save partial AI history.

Evidence: `docs/evidence/2026-10-07-ai-prompt-truncation.json` and `docs/evidence/2026-10-07-ai-operation-outcomes.json`. Focused AI/API suite: 50 PASS. Exact-source full Python suite: 854 PASS, one optional PostgreSQL skip. Frontend build and agent-skills/catalogue/progress/lane validators, compileall and diff check PASS. Hosted checks/review NOT_RUN_BY_USER_POLICY; external submission OFF.

Issue #381 can be closed with its full local acceptance evidence. This does not change overlapping original requirements MJ-REQ-004/109/110/111.

## Other active lane claims

- Antigravity #302: `services/discovery/sources/**`, `tests/integration/test_dice.py`.
- Ollama #318: `services/ats_detection/**`, `tests/unit/test_ats_detection*`, `tests/integration/test_ats_detection*`.

Release the Codex #381 claim after issue evidence/closure, recheck machine claims, GitHub AI LANE comments and worktrees, then select the next highest-value dependency-ready issue. Actions remain disabled.


## 2026-10-07 active issue #386 / MJ-REF-016

Dedicated worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-ref-016-dead-letter-20261007`, branch `codex/mj-ref-016-dead-letter-20261007`, base main `5d37700bcedf7013fc89968e260454af6fae1817`. Claim mirrored at https://github.com/MulattoTech/MJobs/issues/386#issuecomment-6033066988. Baseline queue/lifecycle 16 tests passed. Exhausted expiry originally persisted FAILED instead of DEAD_LETTERED. Existing operation ID is the dispatcher idempotency identity; append-only audit_events retain retry/discard history. Ambiguous application retry is blocked for reconciliation.

Implementation commit `b778917a56d266a50f3cb1e21c9f4220bb20a28c` is pushed and fast-forwarded to local main. Evidence `docs/evidence/2026-10-07-dead-letter-queue.json`. Exact full suite: 875 Python tests PASS with one optional PostgreSQL concurrency skip; 63 frontend tests/build, connected browser review/actions/reload, SQLite and temporary PostgreSQL 17 migration checks, repository validators PASS. Hosted checks/review remain NOT_RUN_BY_USER_POLICY; external submission OFF. Close issue after origin/main and evidence readback, then release claim/lock and select the next dependency-ready issue.
