# MJobs concurrent Codex + Antigravity implementation contract

**Status:** User-authorized two interactive local coding sessions. This file coordinates delivery; it does **not** launch agents or authorize GitHub-hosted workers, Actions, hosted reviews, hosted CI, or unattended work. `AGENTS.md` remains authoritative. User authorization dated 2026-10-09 supersedes the older *single active local agent session* restriction only for one Codex implementation root and one Antigravity implementation root. There is still exactly **one integrator**.

## Desired outcome and order

1. **Codex — application automation:** prioritize `MJ-REQ-122` / [#123](https://github.com/MulattoTech/MJobs/issues/123) and bounded Greenhouse [#346](https://github.com/MulattoTech/MJobs/issues/346) / Lever [#347](https://github.com/MulattoTech/MJobs/issues/347) adapters if genuinely dependency-ready; then the generic form engine [#129](https://github.com/MulattoTech/MJobs/issues/129), durable application state [#138](https://github.com/MulattoTech/MJobs/issues/138) / [#139](https://github.com/MulattoTech/MJobs/issues/139), and review / preview / ambiguity / idempotency / recovery. Deliver user-visible, verified **review-first** workflows, not skeletons. Core external submission must remain OFF.
2. **Antigravity — AI matching:** prioritize explainable score engine [#113](https://github.com/MulattoTech/MJobs/issues/113), components [#114](https://github.com/MulattoTech/MJobs/issues/114), explanation [#115](https://github.com/MulattoTech/MJobs/issues/115), required-vs-preferred [#116](https://github.com/MulattoTech/MJobs/issues/116), and weight tuning [#118](https://github.com/MulattoTech/MJobs/issues/118). Deliver deterministic, confidence-aware, citation/fact-grounded scoring with honest unknowns; optional AI can refine, never invent facts.
3. **Integrator — P0 delivery control:** maintain the exact acceptance matrix [#292](https://github.com/MulattoTech/MJobs/issues/292) and end-to-end product acceptance [#293](https://github.com/MulattoTech/MJobs/issues/293). First build a reusable local fixture-based end-to-end user journey joining the two lanes after both are accepted. Do not award full P0 PASS unless the complete 60-step contract has evidence. Integrator owns canonical metadata and merges.

The reference starting point on 2026-10-09 was application automation 0/50 PASS, matching AI 2/19 PASS, delivery control 0/11 PASS. These figures are **historical**, not a fresh checkpoint. Re-read the canonical matrices before selecting work. Parent requirements are not PASS merely because a child slice shipped.

## Hard isolation and ownership

- **Separate checkouts:** Codex uses `C:\Tools\Dev\MJobs-Codex-App` on a unique `codex/priority-app-automation-...` branch. Antigravity uses `C:\Tools\Dev\MJobs-Antigravity-Matching` on a unique `antigravity/priority-matching-ai-...` branch. Neither edits the primary checkout, the other worktree, or the other branch. Both branch from the **verified latest `origin/main`** after integration.
- **Codex allowed implementation area:** `services/applications/**`, `workers/applications/**`, related *exclusively claimed* tests under `tests/unit/**`, `tests/integration/**`, `tests/browser/**`, and new uniquely named sanitized evidence. Use only specific explicit paths in each claim; do not claim entire test directories. Existing files may be edited only if the real claim bus and other lane's issue comments show no overlap.
- **Antigravity allowed implementation area:** `services/matching/**`, scoped `services/ai/**` only when not also used by the application workflow, *exclusively claimed* matching tests, and new self-contained matching UI components. Do not edit shared application UI mounts or API routers without integrator handoff.
- **Integrator only:** `AGENTS.md`, `GEMINI.md`, `CLAUDE.md`, `.agents/rules/**`, `.github/**`, `README.md`, `docs/PROGRESS_*`, `planning/**` other than each agent's own `planning/lanes/<lane>/**`, `migrations/**`, shared persistence/domain contracts, application mounting/composition, Compose, repo-level lock files, and root `main`. Integration changes to shared APIs are serialized after component lanes stop editing affected files. No lane touches these without an explicit revised claim from integrator.
- **External existing claims:** preserve `antigravity` issue #302 (Dice source adapter) and `ollama` issue #318 (ATS detection) if still active; they are *not* part of the new two-lane scope. Treat all active claims as occupied until individually inspected or formally released. Do not override stale-looking claims because their PID is empty.
- **Collision protocol:** if the desired file/issue is claimed, stop *that slice*, pick a different nonoverlapping issue/path from the same priority area, or ask the integrator to sequence an interface. No forced release, silent takeover, automatic rebase of somebody else's work, or speculative merge.

## Before modifying a line of code (each session)

1. Read `AGENTS.md`, `docs/DUAL_AGENT_COORDINATION.md`, `planning/lanes/README.md`, `docs/PROGRESS_TRACKING.md`, its current GitHub issue, exact area spec and current code.
2. Verify branch, `git status --porcelain`, `git fetch origin main`, branch base ancestry and `git ls-remote origin refs/heads/main`. Do not reset/rebase dirty unrelated trees. Use `git worktree list` to confirm the correct checkout.
3. Run `python -m tools.coordination.lane_coordination validate` and `... status`. Claim **specific** paths with `claim --lane codex|antigravity --child-id <unique-id> --issue <issue> --requirement <MJ-REQ-ID> --branch <branch> --worktree <path> --path <one-exact-file>`; use additional `--path` as supported by `--help`. If claim rejects, do not bypass it.
4. Mirror a sanitized claim on the owning GitHub issue: agent lane, branch, exact base SHA, issue/requirement, file paths, estimated slice, expiry/check-in expectation. Issue comments are the cross-machine coordination channel, not an automatic distributed lock.
5. Implement one substantive vertical slice, run the affected tests/build (including negative/safety cases), record evidence with exact commit/tree, report skips and blockers. Commit **only owned** files to the lane branch. Never `git add -A`; do not edit canonical status matrices.
6. Prepare a concise branch handoff and stop; integrator reviews and merges serially with a local integration lock. Integrator updates accepted status and dashboard exactly once per accepted batch and publishes non-force.

## Integration sequence / release gate

1. Establish idle ownership and locate all dirty/untracked work. Do not clean/delete those files to permit a merge. Verify any candidate commit against current `main`, code diff, acceptance scope, and targeted tests.
2. Acquire the **common Git** integration lock via `python -m tools.coordination.lane_coordination acquire-lock --lane codex` (or designated integration lane). Verify no active owner before calling. No parallel merge owners.
3. Integrate Codex application adapter slice first, then Antigravity matching slice (or reverse only if validated dependencies demand it). Handle shared wiring/migrations in a **separate** integrator-owned commit; run affected integration and synthetic browser tests after each.
4. Update only genuinely proved requirement statuses, with both original and reference denominators kept separate. Run `python -m tools.verification.catalogue`, `python -m tools.verification.progress validate`, `python -m tools.verification.progress_dashboard --write`, then `--check`; run changed-area tests and `git diff --check`.
5. Verify a non-force ancestry-preserving path to GitHub `main`, ensure Actions/hosted agents remain disabled, publish only accepted commits, and confirm local SHA == remote SHA. Then release owned claims and lock. Keep unfinished branches preserved and documented.

## Stop conditions and universal invariants

Stop the specific lane on unowned file changes, active conflicting claim, diverged base with unresolved conflicts, ambiguous credential/real ATS authorization, code-generation factual uncertainty, failed source tests, or lost integration ownership. Handoff without inventing PASS. Never bypass CAPTCHA/MFA/anti-bot, submit external real applications, log personal data, spend on hosted review/CI, or mutate the host drivers/BIOS/network/GPU. Never claim true parallel isolation is guaranteed by Markdown alone: branches + worktrees + precise claims + GitHub issue comments + serial integration are all required.

## Ready-to-paste launcher profiles

See [prompts/dual-agent/README.md](../prompts/dual-agent/README.md). Each model profile contains a copyable `/goal` text and a full task prompt. Model choice changes *execution emphasis*, not ownership or safety. Treat `/goal` as text if the client does not support that slash command.
