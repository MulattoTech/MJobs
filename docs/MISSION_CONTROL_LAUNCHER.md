# MJobs Mission Control — one-click web launcher and release tracker

## Start the app

Double-click **MJobs Mission Control** on the Windows desktop. The shortcut invokes the checked-in `Launch-MJobs-MissionControl.vbs` (hidden; no console flashes) and `Start-MJobs-MissionControl.ps1`, which starts a loopback-only FastAPI dashboard at **http://127.0.0.1:18726/** if it is not already running, then opens that page in the default browser. Running again opens the existing instance. The bootstrap and server logs stay under the local common Git directory `.git/mjobs-coordination/`; they are not committed.

To start manually:

```powershell
cd C:\Tools\Dev\MJobs
.\Start-MJobs-MissionControl.ps1
```

**No CLI sessions are started merely by opening the dashboard.** The user must click **Launch CLI** on each card.

## Three supported launch slots

| Web slot | Installed runner | Isolated checkout | Scope |
|---|---|---|---|
| Codex | `codex exec` | `C:\Tools\Dev\MJobs-Codex-App` | review-first application automation |
| Antigravity | `agy --print` | `C:\Tools\Dev\MJobs-Antigravity-Matching` | evidence-backed AI matching |
| Third CLI | `claude --print` | `C:\Tools\Dev\MJobs-Claude-Delivery` | independent local delivery/test acceptance |

Models/profiles are selected visually; CLI agent names and extra instructions are optional. Each card has **Read prompt**, **Launch CLI** and **Stop**, plus an opt-in auto-continue toggle, off by default. The first two slots load the seven existing GitHub-published prompt profiles. Claude adds release audit and test-harness profiles. For Codex Sol 6.1/Astra, enter the exact model ID reported by Codex rather than guessing an alias. Antigravity's installed CLI model list (as checked on 2026-10-09) includes `claude-opus-4-6-thinking`, `claude-sonnet-4-6`, `gemini-3.8-flash-medium`, and `gemini-3.8-flash-high`. A listed model can become unavailable due to provider/session limits; an error must be shown, not hidden.

The legacy Ollama/OpenCode lane remains visible in **Advanced agent telemetry** only. When its executables are absent, the UI reports it as unavailable and never reinstalls them.

## Clean-main independence and self-reconciliation

**The launcher does not require a clean, current, or synchronized `main`.** It never resets, cleans, stashes, commits, rebases, merges, or force-pushes the primary checkout. At each explicit launch it:

1. Tries a bounded fetch of remote `main`; if offline, chooses the cached `origin/main`, falling back to locally committed main and warns.
2. Reuses the lane's designated worktree if the repository identity and branch match. Existing dirty/ignored data is preserved. A foreign directory or unexpected branch blocks that lane.
3. Creates a **missing** worktree from the selected base without switching primary `main`. A clean branch that is only behind may be fast-forwarded. Dirty or diverged branches are left intact, with the warning shown to the user.
4. Records the chosen model, agent, profile and prompt in **local** common Git dashboard settings; starts the exact installed CLI with bounded local worktree context.
5. Instructs the launched model to check real GitHub issues/claims, take a unique path-scoped claim, and wait for the sole integrator to update accepted requirements and merge to main. No hidden automatic integration.

A launched CLI is **not** a license to override another writer's claim. Claim collisions block that work slice. The new Claude lane has its own claim namespace. Antigravity and Claude can now use unique child/session claim IDs without overwriting pre-existing legacy claims.

## Current release acceptance

The Release Acceptance panel uses `tools.verification.progress_dashboard.snapshot()`, **the same canonical** `planning/CAPABILITY_MATRIX.json` and `planning/REFERENCE_GAP_MATRIX.json` that drive the portable ChatGPT skill. It displays only whole requirements with fully accepted evidence, side-by-side counts of PARTIAL and NOT_RUN, acceptance by area and priority, latest matrix timestamps, current milestone and separate release-readiness state.

The browser refreshes its read-only status approximately every 11 seconds while visible. The remote `main` SHA is checked on a short cache to avoid excessive network requests. `checkpoint_current` distinguishes a rendered GitHub/Markdown/SVG checkpoint from live local matrix changes. **The page never edits matrices, fabricates successful tests, or automatically publishes to GitHub.** An integration owner must refresh/publish accepted checkpoints according to `docs/PROGRESS_TRACKING.md`.

## Safety and operational notes

The service binds only to 127.0.0.1 and checks its own health identity before opening a port. Mutation endpoints require a per-process token and same-origin requests. Process runner names are allowlisted and launch profile files are fixed repository-relative entries, not arbitrary executable paths. If a CLI is already active in a launch slot, Start refuses to take it over; use Stop and launch again after verifying the prior work is settled.

CLI processes may consume account/API quotas when manually launched. Their availability can differ from a listed model name. Preserve logs and actual validation; hosted CI/reviews and external real job submission remain OFF.

Run verification:

```powershell
python -m unittest tests.unit.test_mission_control tests.unit.test_agent_dashboard_api tests.unit.test_agent_dashboard tests.unit.test_lane_coordination -v
python -m tools.verification.progress_dashboard --check
python -m tools.coordination.lane_coordination validate
```
