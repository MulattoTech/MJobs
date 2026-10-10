# MJobs Claude CLI / Sonnet — release acceptance and test gaps

## Copyable /goal

```text
/goal Independently inspect the 60-step end-to-end MJobs acceptance contract, run safely available local tests, and identify evidence-backed blockers by requirement without claiming false PASS.
```

## Implementation task

Operate only in your dedicated **Claude** worktree. Read AGENTS.md, docs/DUAL_AGENT_COORDINATION.md, planning/lanes/README.md and docs/spec/areas/delivery_control.md. Focus GitHub #293 / MJ-REQ-292 and #292 / MJ-REQ-291. Validate actual user-visible workflows, synthetic fixtures and local E2E tests without external submissions. If adding tests, first claim exact unique test files using `python -m tools.coordination.lane_coordination claim --lane claude --child-id <unique> --issue 293 --requirement MJ-REQ-292 --branch <your-branch> --worktree <your-path> --path <unique-test>`, then mirror claim on GitHub. No root main edits, canonical acceptance matrix updates, shared file changes, or hosted CI. Commit test-only work on your branch for the single integration owner to review. Report verified counts, exact commands, original/reference acceptance limitations, gaps, and clean handoff.
