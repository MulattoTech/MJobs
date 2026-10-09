# MJobs status card in ChatGPT and GitHub

Ask **“Show MJobs progress”** in any ChatGPT chat with the GitHub/Remote Desktop Commander connections available. The tiny docs/PROGRESS_CARD.json is quick to read. If the local MJobs checkout is connected, prefer its verified canonical data over a GitHub snapshot, because local main can be ahead of the remote.

## How the graphic updates

At an **integration-owner-approved accepted batch**, run the existing one-command tracker update:

    cd C:\Tools\Dev\MJobs
    python -m tools.verification.progress_dashboard --write
    python -m tools.verification.progress_dashboard --check

--write now updates the existing dashboard *and* the matching small JSON/SVG card. Repeating it without changed accepted evidence is a no-op. --check verifies all three outputs without modifying anything. The integration owner then commits/pushes through the repository's existing authorized publication policy. Nothing runs on a timer, hook or hosted CI.

- docs/PROGRESS_CARD.svg: dark progress-bar graphic, suitable for embedding in GitHub README.
- docs/PROGRESS_CARD.json: schema-versioned counts, original/reference timestamps, milestone, release status. Designed for ChatGPT to fetch quickly.
- docs/PROGRESS_DASHBOARD.md: full matrix counts by priority and area.
- skills/mjobs-progress/SKILL.md: portable prompt workflow for ChatGPT Skills and compatible agent environments.

Only PASS counts toward the percentage; PARTIAL and NOT_RUN are displayed but do not imply degrees of code completion. The as-of timestamp comes from canonical acceptance evidence, not from when someone opened the graphic. A developer may have committed hundreds of changes locally without publishing them to the private GitHub repository; a GitHub card cannot reflect work that has not been pushed.

## Using the ChatGPT skill

If your ChatGPT plan/workspace exposes skills, navigate to **Plugins → Skills → Create → Upload from your computer**, upload mjobs-progress-skill.zip, and use the phrase “Show MJobs progress”. Availability differs by plan; the skill package doesn't silently install itself or grant GitHub/desktop permissions. If Skills are unavailable, the same phrase still works in a conversation where ChatGPT has access to the connected apps.

## Safe integration

This feature branch can be reviewed/cherry-picked into the current **local** MJobs main when the root integrator releases its lock. Before merging, reconcile the long-lived local-versus-remote Git divergence and update generated JSON/SVG via --write from fresh local matrices. Do **not** overwrite docs/PROGRESS_DASHBOARD.md or planning matrices to match GitHub's older published numbers. Never turn on GitHub Actions, cloud reviewers, external submit, or automatic pushes as part of this feature.
