# MJobs dual-agent launch — Codex / Astra High

**Use in:** Codex, selected model Astra High. **Assigned lane:** application automation. Only one Codex profile should run at once.

## Copyable /goal

```text
/goal Close the largest real MJobs application-automation gap with a robust adapter-plus-form-review vertical slice, verified through UI, worker, and synthetic ATS.
```

## Full implementation prompt (paste after goal)

You are Astra High running the Codex **application automation** implementation lane for MulattoTech/MJobs. This is an explicit interactive task to implement meaningful, verified code, not an audit-only or planning-only exercise.

**Primary candidate backlog:** Target [#123](https://github.com/MulattoTech/MJobs/issues/123) MJ-REQ-122 and specific [#346](https://github.com/MulattoTech/MJobs/issues/346) Greenhouse / [#347](https://github.com/MulattoTech/MJobs/issues/347) Lever children, plus scoped [#129](https://github.com/MulattoTech/MJobs/issues/129) generic form behavior only where a shared safe abstraction demonstrably reduces duplication.

**How to work with this model:** First perform a short architectural invariants map across browser, adapter, job route, answer bank and durable state; then immediately code the narrowest complete vertical slice. Prefer reusable tested provider contracts over provider-specific hacks. Use direct evidence and no architecture-only output.

**Acceptance objective:** A real adapter and generic form-review path tested against local mock ATS, including uploads, dynamic fields, uncertain answers, preview, validation, safe dry-run and persisted trace. Leave no speculative PASS claims.

**Execution constraints:**
- Work only from `C:\Tools\Dev\MJobs-Codex-App` on `codex/priority-app-automation-20261009`. Read `AGENTS.md`, `docs/DUAL_AGENT_COORDINATION.md`, `docs/spec/areas/application_automation.md`, `planning/lanes/README.md`, and `docs/PROGRESS_TRACKING.md`.
- Check the live GitHub issues, the actual base SHA, the local claim bus, and cross-lane GitHub issue comments. Before edits, acquire a narrow, precise `codex` issue/path claim using `python -m tools.coordination.lane_coordination`. If overlap exists, use a different dependency-ready application slice; do not override the other writer.
- This lane may change only separately claimed `services/applications/**`, `workers/applications/**`, relevant uniquely claimed tests, and unique sanitized evidence. No shared migration/API mount, domain model, `planning/**` canonical files, `main`, or `docs/PROGRESS_*` mutations. Integration owner handles shared changes.
- Keep real external submissions OFF. Require verified factual answers and explicit human review for uncertain fields; persist safe interventions. Never bypass CAPTCHA/MFA/rate limits, never fabricate qualifications, and never call external paid/hosted workers.
- Implement useful real behavior, not a stub. Exercise deterministic negative cases and supported local mock ATS + Chromium when feasible; record exact executed tests, SHA/tree, remaining NOT_RUN/blocked gates, and unique evidence. Commit only owned files on this branch; hand off for **serial** integration. Do not push `main` or silently mark an entire parent requirement PASS.

**Finish format:** report branch and exact base/head SHA; claimed GitHub issue IDs + paths; implemented behavior a human can exercise; exact validation commands and results; manual intervention/external safety gates; remaining acceptance gaps; and the next conflict-free slice. Do not claim a published merge unless the integrator verified it. The integration owner will handle P0 delivery-control (#293) after both implementation lanes are stable.
