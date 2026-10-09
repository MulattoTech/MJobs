# MJobs dual-agent launch — Antigravity / Gemini 3.8 Flash Medium

**Use in:** Antigravity, selected model Gemini 3.8 Flash Medium. **Assigned lane:** AI matching. Only one Antigravity profile should run at once.

## Copyable /goal

```text
/goal Deliver a precise, test-backed deterministic MJobs match-score component that distinguishes required qualifications, preferred skills, and unknown evidence.
```

## Full implementation prompt (paste after goal)

You are Gemini 3.8 Flash Medium running the Antigravity **AI matching** implementation lane for MulattoTech/MJobs. This is an explicit interactive task to implement meaningful, verified code, not an audit-only or planning-only exercise.

**Primary candidate backlog:** Start with [#114](https://github.com/MulattoTech/MJobs/issues/114) component score and [#116](https://github.com/MulattoTech/MJobs/issues/116) requirement strength. Integrate into [#113](https://github.com/MulattoTech/MJobs/issues/113) only when clear nonoverlapping interfaces are present.

**How to work with this model:** Prefer one bounded change and fast targeted test cycles. Implement explicit typed inputs, factor-level scoring, stable tie/order, missing-vs-failed facts, and negative fixtures; avoid huge sweeping edits, shared migrations, external AI calls or speculative architectural rewrites. Escalate integration blockers rather than guessing.

**Acceptance objective:** A tested, deterministic scoring module with clear reasoning for each input and no fabricated facts. Integration instructions and evidence included.

**Execution constraints:**
- Work only from `C:\Tools\Dev\MJobs-Antigravity-Matching` on `antigravity/priority-matching-ai-20261009`. Read `AGENTS.md`, `GEMINI.md`, `.agents/rules/mjobs-parallel-ownership.md`, `docs/DUAL_AGENT_COORDINATION.md`, `docs/spec/areas/matching_ai.md`, `planning/lanes/README.md`, and `docs/PROGRESS_TRACKING.md`.
- Check current GitHub issues, real remote `main` ancestry, machine-local claims, and other lane's GitHub issue comments. Claim **specific files** in the `antigravity` lane before writing. Respect all existing source/ATS claims and all Codex application paths. If overlap exists, select a narrower scoring slice; never steal files or claims.
- Edit only precisely claimed `services/matching/**`, strictly independent `services/ai/**`, matching tests, and new standalone scoring UI components; no shared router mounts, domain/persistence schemas, `migrations/**`, canonical `planning/**`, `docs/PROGRESS_*`, or `main`. Record integration-needed interfaces for the integrator.
- Score exclusively against persisted, verified career facts and attributed job evidence. Missing skills/unknown salary/remote constraints must remain unknown—not invented. Deterministic fallback works without AI provider, extra spend, or GPU.
- Deliver a material tested product slice, including structured score breakdown, counterexamples, provenance, regressions, and truthful blockers. Commit only owned files and unique sanitized evidence to the Antigravity branch, then hand off for serial integration. Hosted CI/agents and real external application submission remain OFF.

**Finish format:** report branch and exact base/head SHA; claimed GitHub issue IDs + paths; implemented behavior a human can exercise; exact validation commands and results; manual intervention/external safety gates; remaining acceptance gaps; and the next conflict-free slice. Do not claim a published merge unless the integrator verified it. The integration owner will handle P0 delivery-control (#293) after both implementation lanes are stable.
