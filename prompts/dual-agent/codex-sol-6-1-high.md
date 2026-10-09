# MJobs dual-agent launch — Codex / Sol 6.1 High

**Use in:** Codex, selected model Sol 6.1 High. **Assigned lane:** application automation. Only one Codex profile should run at once.

## Copyable /goal

```text
/goal Make MJobs application attempts crash-safe and replay-safe, then prove a complete Greenhouse review-first flow end to end against controlled fixtures.
```

## Full implementation prompt (paste after goal)

You are Sol 6.1 High running the Codex **application automation** implementation lane for MulattoTech/MJobs. This is an explicit interactive task to implement meaningful, verified code, not an audit-only or planning-only exercise.

**Primary candidate backlog:** Investigate [#138](https://github.com/MulattoTech/MJobs/issues/138) MJ-REQ-137 state machine and [#139](https://github.com/MulattoTech/MJobs/issues/139) MJ-REQ-138 attempt model, using [#146](https://github.com/MulattoTech/MJobs/issues/146) idempotency and [#346](https://github.com/MulattoTech/MJobs/issues/346) Greenhouse adapter as connected downstream proof. Choose exact dependency-ready issue(s) rather than claiming four simultaneously.

**How to work with this model:** Front-load the hardest invariants: immutable attempt identity, idempotent retry, durable step/trace, ambiguous submit => SUBMISSION_UNCONFIRMED, no duplicate external action, safe browser-resume semantics and policy gates. Challenge race and failure assumptions with negative/fault-injection tests before adding more providers.

**Acceptance objective:** One proven end-to-end safe attempt lifecycle with intentional interruption and restart; attach exact test coverage and failures. Keep broader requirements PARTIAL unless fully met.

**Execution constraints:**
- Work only from `C:\Tools\Dev\MJobs-Codex-App` on `codex/priority-app-automation-20261009`. Read `AGENTS.md`, `docs/DUAL_AGENT_COORDINATION.md`, `docs/spec/areas/application_automation.md`, `planning/lanes/README.md`, and `docs/PROGRESS_TRACKING.md`.
- Check the live GitHub issues, the actual base SHA, the local claim bus, and cross-lane GitHub issue comments. Before edits, acquire a narrow, precise `codex` issue/path claim using `python -m tools.coordination.lane_coordination`. If overlap exists, use a different dependency-ready application slice; do not override the other writer.
- This lane may change only separately claimed `services/applications/**`, `workers/applications/**`, relevant uniquely claimed tests, and unique sanitized evidence. No shared migration/API mount, domain model, `planning/**` canonical files, `main`, or `docs/PROGRESS_*` mutations. Integration owner handles shared changes.
- Keep real external submissions OFF. Require verified factual answers and explicit human review for uncertain fields; persist safe interventions. Never bypass CAPTCHA/MFA/rate limits, never fabricate qualifications, and never call external paid/hosted workers.
- Implement useful real behavior, not a stub. Exercise deterministic negative cases and supported local mock ATS + Chromium when feasible; record exact executed tests, SHA/tree, remaining NOT_RUN/blocked gates, and unique evidence. Commit only owned files on this branch; hand off for **serial** integration. Do not push `main` or silently mark an entire parent requirement PASS.

**Finish format:** report branch and exact base/head SHA; claimed GitHub issue IDs + paths; implemented behavior a human can exercise; exact validation commands and results; manual intervention/external safety gates; remaining acceptance gaps; and the next conflict-free slice. Do not claim a published merge unless the integrator verified it. The integration owner will handle P0 delivery-control (#293) after both implementation lanes are stable.
