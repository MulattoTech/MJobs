# MJobs dual-agent launch — Antigravity / Opus

**Use in:** Antigravity, selected model Opus. **Assigned lane:** AI matching. Only one Antigravity profile should run at once.

## Copyable /goal

```text
/goal Build an evidence-grounded explainable MJobs job-fit engine that distinguishes must-haves, preferences, missing information, confidence, and reasons behind every score.
```

## Full implementation prompt (paste after goal)

You are Opus running the Antigravity **AI matching** implementation lane for MulattoTech/MJobs. This is an explicit interactive task to implement meaningful, verified code, not an audit-only or planning-only exercise.

**Primary candidate backlog:** Target [#113](https://github.com/MulattoTech/MJobs/issues/113) MJ-REQ-112, [#114](https://github.com/MulattoTech/MJobs/issues/114) MJ-REQ-113 and [#115](https://github.com/MulattoTech/MJobs/issues/115) MJ-REQ-114; include [#116](https://github.com/MulattoTech/MJobs/issues/116) required/preferred distinction only for the bounded owned slice.

**How to work with this model:** Start with a short domain/evidence audit and an explicit scoring contract. Prioritize hard correctness properties (no fabricated expertise, missing-vs-negative, stale fact provenance, typed explanation factors, fixed weights, deterministic replay) over prompt engineering and attractive UI. Then implement deep negative tests and an API-ready independent module.

**Acceptance objective:** For fixed profile/job fixtures, the same score and transparent component factors are returned reproducibly, cite source facts, explain unknowns, and reject unsupported career facts. Optional AI improves only validated input parsing; deterministic fallback remains usable.

**Execution constraints:**
- Work only from `C:\Tools\Dev\MJobs-Antigravity-Matching` on `antigravity/priority-matching-ai-20261009`. Read `AGENTS.md`, `GEMINI.md`, `.agents/rules/mjobs-parallel-ownership.md`, `docs/DUAL_AGENT_COORDINATION.md`, `docs/spec/areas/matching_ai.md`, `planning/lanes/README.md`, and `docs/PROGRESS_TRACKING.md`.
- Check current GitHub issues, real remote `main` ancestry, machine-local claims, and other lane's GitHub issue comments. Claim **specific files** in the `antigravity` lane before writing. Respect all existing source/ATS claims and all Codex application paths. If overlap exists, select a narrower scoring slice; never steal files or claims.
- Edit only precisely claimed `services/matching/**`, strictly independent `services/ai/**`, matching tests, and new standalone scoring UI components; no shared router mounts, domain/persistence schemas, `migrations/**`, canonical `planning/**`, `docs/PROGRESS_*`, or `main`. Record integration-needed interfaces for the integrator.
- Score exclusively against persisted, verified career facts and attributed job evidence. Missing skills/unknown salary/remote constraints must remain unknown—not invented. Deterministic fallback works without AI provider, extra spend, or GPU.
- Deliver a material tested product slice, including structured score breakdown, counterexamples, provenance, regressions, and truthful blockers. Commit only owned files and unique sanitized evidence to the Antigravity branch, then hand off for serial integration. Hosted CI/agents and real external application submission remain OFF.

**Finish format:** report branch and exact base/head SHA; claimed GitHub issue IDs + paths; implemented behavior a human can exercise; exact validation commands and results; manual intervention/external safety gates; remaining acceptance gaps; and the next conflict-free slice. Do not claim a published merge unless the integrator verified it. The integration owner will handle P0 delivery-control (#293) after both implementation lanes are stable.
