---
name: mjobs-progress
description: Show a compact, accurate MJobs acceptance-progress card whenever the user asks for MJobs progress, completion, requirement acceptance, or a status graphic. Read local canonical data when connected; otherwise use the GitHub checkpoint. Never fabricate fresh acceptance or launch changes.
---

# MJobs progress: fast, trustworthy status card

Invoke for “Show MJobs progress”, “MJobs status”, “how complete is M-Tech Jobs?”, “show the MJobs progress bar”, or an equivalent request. For a normal progress request, **read only**; never start Codex, change models, regenerate a tracker, push, or edit the repository.

Repository: private MulattoTech/MJobs. Local primary checkout (if authorized/online): C:\Tools\Dev\MJobs. The user has GitHub and Remote Desktop Commander connections; use them only when currently available/authorized. Do not expose secrets or private local data in the response.

## Fast retrieval (prefer one read)

1. If MulattoTechBox is connected, read local docs/PROGRESS_CARD.json and check that it corresponds to the canonical matrices using the non-mutating command python -m tools.verification.progress_dashboard --check from the repository root. If current, display these numbers as **LOCAL VERIFIED**, with original.as_of as the acceptance evidence timestamp. For current installs without this new JSON file, run python -m tools.verification.progress_dashboard --summary and read planning/CAPABILITY_MATRIX.json timestamp as needed. If the local card is stale, report the fresh read-only summary, explicitly labeling the card stale.
2. If local is offline/unavailable, read docs/PROGRESS_CARD.json from the main branch through the connected GitHub tool. If missing, read docs/PROGRESS_DASHBOARD.md, using only its exact original and reference counts and source timestamps. Label this **PUBLISHED GITHUB CHECKPOINT**, never live local progress.
3. If the user asks about sync lag, compare local git rev-parse HEAD against GitHub main HEAD. Do not assume origin/main was recently fetched. Say when local is ahead without guessing exact ahead-by numbers unless ancestry is verified. Never automatically push commits.
4. If no authorized source is reachable, explain that current numbers are unavailable and point to the last verified checkpoint only if it is available in this conversation. Never silently reuse a stale percentage as live.

## Rendering

Show a compact dark-style ChatGPT card with a green horizontal bar PASS / total (only fully accepted requirements fill the bar), one headline PASS / total, PARTIAL and NOT_RUN counts, percentage to one decimal place, source label and as-of timestamp. Explicitly separate MJ-REQ original requirements from MJ-REF additional reference gaps. On request show reference gaps, per-area details, or how numbers changed since a prior checkpoint. Render directly with ChatGPT-native text/layout; you do not need a generated screenshot or an image-generation call.

If the environment cannot render an interactive card, show a compact Markdown progress bar and exact counts. Do not equate PARTIAL to fractional completion, NOT_RUN to unimplemented, or a local test to full release readiness. Accept only canonical statuses backed by the matrices; do not infer successes from commits, a PR merge, GitHub issue closure, AI claims, or unmerged work.

## Updates and provenance

The repository's tools/verification/progress_dashboard.py --write regenerates docs/PROGRESS_DASHBOARD.md, docs/PROGRESS_CARD.json, and docs/PROGRESS_CARD.svg **only at integration-owner-approved accepted checkpoints**. It does not run in the background. The integration owner decides whether or when to push local commits to GitHub, independently of displaying local progress. Do not enable GitHub Actions, cloud CI, hooks, timers, watchers, real ATS submissions, or overwrite root-owned planning files. See docs/PROGRESS_TRACKING.md for authority and write constraints. On user requests to update the card on GitHub, first check local/GitHub divergence and current integration lock, and publish only when explicitly authorized and safe.

## Quick answer example

**MJobs — LOCAL VERIFIED (as of {matrix timestamp})**

**100 / 366** original requirements fully accepted — **27.3%**

Green progress bar · **101** partial · **165** not yet validated.

Additional reference gaps and GitHub sync status only when available. Values here are an example, not permanent data.
