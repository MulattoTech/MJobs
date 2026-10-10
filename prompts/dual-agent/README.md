# Start MJobs Codex and Antigravity independently

Use **one** Codex model/profile and **one** Antigravity model/profile at a time. These profiles are mutually alternative *model choices*, not seven agents to run at once. All are bound by `AGENTS.md` and `docs/DUAL_AGENT_COORDINATION.md`.

## One-time working-directory preparation (integration owner)

**Recommended:** double-click the **MJobs Mission Control** desktop shortcut and use the web-based Launch Studio instead of running these commands by hand. The launcher creates missing worktrees, reuses existing lane work, fetches recent GitHub state when available, and never needs root `main` to be clean. The manual commands below remain for advanced use and assume a fresh setup.

```powershell
cd C:\Tools\Dev\MJobs
git fetch origin main
git worktree add -b codex/priority-app-automation-20261009 C:\Tools\Dev\MJobs-Codex-App origin/main
git worktree add -b antigravity/priority-matching-ai-20261009 C:\Tools\Dev\MJobs-Antigravity-Matching origin/main
```

If either worktree already exists, **do not run that add again**; inspect it. Open Codex in `C:\Tools\Dev\MJobs-Codex-App`, and Antigravity in `C:\Tools\Dev\MJobs-Antigravity-Matching`. The project root is **not** either agent's implementation workspace. The agent's model selector determines actual availability; these files do not install or select a model.

## Model-specific files

| App | Choose **one** profile | Focus |
|---|---|---|
| Codex | [Luna High](codex-luna-high.md) | review-first ATS vertical slice and durable state |
| Codex | [Sol 6.1 High](codex-sol-6-1-high.md) | hardest safety/idempotency invariants and adapter integration |
| Codex | [Astra High](codex-astra-high.md) | architectural cross-check + deep workflow acceptance |
| Antigravity | [Opus](antigravity-opus.md) | explainable scoring and evidence semantics |
| Antigravity | [Sonnet](antigravity-sonnet.md) | implementation and test throughput |
| Antigravity | [Gemini 3.8 Flash Medium](antigravity-gemini-3-8-flash-medium.md) | bounded deterministic scoring improvements |
| Antigravity | [Gemini 3.8 Flash High](antigravity-gemini-3-8-flash-high.md) | full score/weight vertical slice |

Each profile contains both the **/goal** sentence and the **full prompt**. Paste both in the model's chat; if `/goal` is not a recognized command, paste the goal without relying on special processing. Initial named issues are candidates: the agent must verify current availability, dependencies, and claims before changing code.

## After both lanes

The designated integration owner alone takes the lock, reviews both exact branch commits, serializes any shared wiring, runs the local acceptance tests, updates the canonical progress matrix, pushes accepted `main`, and confirms the remote hash. Next, prioritize P0 **MJ-REQ-292** / [issue #293](https://github.com/MulattoTech/MJobs/issues/293) as an actual reproducible end-to-end product acceptance run, not paperwork-only PASS. Real employer submissions remain OFF.


## Third usable CLI lane — Claude Code

The web Launch Studio also supports installed `claude` with independent
`claude/priority-delivery-tests-20261009` worktree, using
[delivery release audit](claude-release-audit.md) or
[controlled test harness](claude-test-harness.md).
It operates under distinct claims and cannot mutate root `main` or canonical
matrices; the sole integration owner performs final release acceptance.
