# Gemini / Antigravity — MJobs

Read [AGENTS.md](AGENTS.md) and [docs/PROGRESS_TRACKING.md](docs/PROGRESS_TRACKING.md) before any mutation or handoff. Respect the lane coordination/claims and the integration lock; only the integration owner refreshes canonical requirements and the generated dashboard after accepted batches or closeout. No per-turn tracker updates, hosted AI/CI, model escalation, or unauthorized background work. Preserve all safety boundaries and untracked work.


## Paired Codex + Antigravity implementation

When the user invokes the dual-agent workflow, follow `docs/DUAL_AGENT_COORDINATION.md` and the automatically loaded `.agents/rules/mjobs-parallel-ownership.md`. Antigravity's sole product lane is matching/AI: operate in the separate `MJobs-Antigravity-Matching` checkout, claim precise files and mirror the claim to the owning GitHub issue. Codex owns application-automation paths. Both must leave canonical planning, shared migrations, dashboard, and `main` to the sole integrator. Launch guidance and Opus/Sonnet/Gemini model-specific prompts: `prompts/dual-agent/README.md`.