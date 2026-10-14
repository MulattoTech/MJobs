# MJobs — start the next original P0/P1 Codex campaign

Open only the canonical repository root: C:\Tools\Dev\MJobs. VS Code should show the root main branch; archived clones/worktrees remain preserved beneath the root and hidden from the normal explorer.

Before a NEW campaign, inspect AGENTS.md, planning/SESSION_HANDOFF.md, live Project #1 and any pending PRs. The last Round3 product PR #654 delivered MJ-REQ-257 PASS while MJ-REQ-014 remains PARTIAL. Never resume its completed interactive session or reuse its old queue.

From a normal (not Administrator) PowerShell or the VS Code terminal at that root, run a read-only preview:

    pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1 -Preview

When that validates authentication, actual prompt paths, P0/P1-only eligibility and live PRs, start a **new** native Codex TUI:

    pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1 -NewCampaign -Interactive -Model gpt-6.1-sol -Reasoning high -AgentThreads 4

This reviewed launcher reads the EXISTING plan, goal and main implementation directive from:
- prompts/codex/next-plan.md
- prompts/codex/next-goal.md
- prompts/codex/next-main.md

It refuses missing/renamed prompt/authority files rather than inventing stale paths. It creates only a new independent checkout INSIDE the MJobs root under .mjobs-recovery/active, keeps the canonical root clean, and leaves the final TUI's model, subagents and usage view interactive. All running work must have one integration owner.

Work only on original incomplete P0/P1 Project #1 requirements. Implement complete vertical user capabilities, verify real connected behavior, then publish through protected PRs, reconcile affected requirement PASS honestly, and archive/retire only safe owned temporary work. No real external job submission, host reboot, OS/network change, secrets exposure, unverified deletion or force push. Optional -Yolo deliberately bypasses the sandbox and is NOT the recommended default.

The GitHub history and release acceptance views are in Mission Control at http://127.0.0.1:18729/ . A changed requirement is not PASS merely because the old run exited, a child PR merged, or the tracker shows a green increment.
