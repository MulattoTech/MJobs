# Implement guided-review issues in feature-completeness order

Use GPT-6 Luna High. This is an implementation turn after a guided review has already been converted into GitHub issues.

Read and obey AGENTS.md and prompts/LOCAL_ONLY_LUNA_ITERATION.md, then reconcile current main, current issue state, planning matrices, and the issue ordering produced by the latest feedback-triage turn.

## Objective

Fix dependency-ready guided-review issues in the order that produces the most feature-rich and feature-complete usable MJobs experience.

Priority:

1. safety/privacy/data integrity/truthfulness;
2. blockers preventing an entire meaningful workflow;
3. prerequisites unlocking multiple downstream issues;
4. Profile → Intake → Match/Search → Documents → Answers → Tracking → Recovery core gaps;
5. reliability/performance;
6. high-friction usability;
7. polish.

Do not blindly follow issue number order or choose cosmetic work because it is easier.

## Local-only budget boundary

GitHub Actions, hosted/cloud CI, cloud agents, bot reviews, and extra AI reviewer sessions remain disabled. Do not enable or rerun them. Do not buy credits, raise spending limits, start Codespaces, or substitute paid provider/model calls.

Use explicit deterministic local tests/builds and isolated synthetic browser/database acceptance. Missing hosted/independent checks remain NOT_RUN rather than being relabeled PASS.

## Loop

For each selected issue:

1. verify checkout ownership and current GitHub truth;
2. read the issue, owning requirement/spec, relevant code/tests/evidence;
3. define the decisive user-visible acceptance oracle;
4. reproduce RED when practical;
5. implement the smallest complete UI/service/storage/history change;
6. validate focused tests plus affected browser/integration behavior;
7. self-inspect for truthfulness, privacy, stale approval, history loss, cross-profile leakage, concurrency, and false-positive tests;
8. record sanitized evidence in the existing planning/evidence system;
9. commit/push the coherent checkpoint and integrate under the documented local-only workflow;
10. close only the issue whose full acceptance is evidenced; keep broader requirements PARTIAL when appropriate;
11. move to the next dependency-ready issue without asking for routine continuation.

After at most three completed issues or four hours, synchronize and leave an exact handoff with the next ordered issues. External submission remains OFF.
