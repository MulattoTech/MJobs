# MJobs Claude CLI / Opus — delivery test harness improvements

## Copyable /goal

```text
/goal Build a reliable deterministic MJobs end-to-end fixture test harness that proves real user-visible job preparation and review-first application behavior across restart/recovery.
```

## Implementation task

In your isolated Claude worktree, read AGENTS.md, docs/DUAL_AGENT_COORDINATION.md, planning/lanes/README.md, docs/spec/areas/test_harness.md and docs/spec/areas/delivery_control.md. Candidate issues: #293 MJ-REQ-292, #292 MJ-REQ-291. Identify an independent test-only slice free of Codex/Antigravity claims. Before editing, claim exact files with the `claude` lane and unique child ID; mirror the claim on GitHub. Write a meaningful stable local synthetic browser/Compose fixture acceptance slice and regressions, run it, record actual output and evidence in unique sanitized files. Do not touch shared API modules, canonical matrices, root main, CI settings, real ATS endpoints, or user secrets. Handoff the verified branch to the integrator; do not claim full requirement PASS just because a portion of the journey works.
