# Native preference-weighting checkpoint

Issue #118 / MJ-REQ-117; related explainability #115 / MJ-REQ-114. Both requirements remain PARTIAL.

- Worktree: `C:/Tools/Dev/MJobs-WebMatching-20261010`
- Branch: `chatgpt/preference-weighting-20261010`
- Tested source: `d2bccd8d7eaf01ec1e6c3986a7d99d79d513ee03`; tree `6e26f9c461873432597b2d585e57fb1160f4934e`; base `f399e00da63c7bf14cd5281b71bba5daf8d62e10`.
- Behavior: five opt-in deterministic preference weights, reserved against source-score injection, unknown-neutral, durable ranked results and explanations.
- Verified: 109 Python/browser tests plus 52 subtests; 569 frontend tests in 100 files; build, compilation, lane and diff validation PASS. No failures or skips in that checkpoint.
- Evidence: `docs/evidence/2026-10-10-preference-weighting.json` contains exact commands, hashes, limits and red-first failures.
- Integration: BLOCKED_BY_ACTIVE_INTEGRATOR. No main, root API, worker or canonical planning paths were edited. TRACKER_REFRESH_PENDING.
- Publication: checkpoint prepared for non-force publication; exact remote receipt belongs in the issue thread after readback.
- Next task: a bounded no-write draft-weight preview using the already mounted scoring-profile router and editor. Claim exact additional paths before implementation.
- Safety: no external application, AI/provider call, hosted execution, personal data export or host configuration change.

The legitimate integrator must inspect current ancestry, rerun the listed scoped tests on the integration candidate, preserve PARTIAL, and update the canonical tracker only under its lock. Do not overwrite another branch or take an occupied integration lock.
