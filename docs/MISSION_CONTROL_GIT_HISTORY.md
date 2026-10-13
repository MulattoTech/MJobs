# Mission Control: three different commit histories

The **Development history** panel is *not* the release acceptance tracker.
It has three explicitly labeled read-only sources:

| Source selector | What it reads | What it cannot prove |
| --- | --- | --- |
| **All known branches (default)** | `git log --all` in the authorized MJobs checkout, including local branch and cached remote-tracking refs across worktrees | Does not distinguish unpushed commits from published ones; remote-tracking refs may need a normal fetch to reflect other machines |
| **Current checkout** | `git log` from the currently checked-out MJobs branch | Commits on other branches are not visible |
| **GitHub main** | GitHub's live `main` commit API via authenticated `gh api`, cached for ~45 seconds | PR branch commits are not shown until merged; changed paths, implementation category and line counts are not examined |

The page polls every five seconds while visible. Switching sources, or using
**Refresh history**, updates the selected source. The GitHub API source is
cached for 45 seconds between ordinary polls to avoid excessive requests;
a manual refresh bypasses that cache. All views are **read-only**: no
`git fetch`, `git reset`, file changes, agents or GitHub mutations.

If the checkout is stuck on an old branch, select **All known branches** to
see commits reachable from other local branch refs. To see only work already
published to the default branch, select **GitHub main**. A commit pushed to
a feature branch but not merged will *not* appear under GitHub main.

The Release acceptance panel is independently computed from MJobs'
canonical requirements. New Git commits, successful PR merges and passing CI
do not automatically turn a requirement into `PASS`; every acceptance
criterion needs its own evidence.

## Correct Git metadata

The release panel shows:
- **Local main**: the local `refs/heads/main` commit
- **GitHub main**: live `origin` main SHA queried without changing refs
- **Checkout**: active working branch and its HEAD, which can be different

A dirty checkout is preserved and explicitly identified, not reset or
silently synchronized. Only the GitHub main history source is authoritative
for published default-branch commits; the all-refs view can include local-only
work.

## Test the selected source

From the repository root:

```powershell
python -m pytest -q tests/unit/test_work_history.py tests/unit/test_work_history_sources.py tests/unit/test_agent_dashboard_api.py
python -m pytest -q tests/browser/test_mission_history_source_selector.py
node --check tools/agent_dashboard/static/history.js
python -m tools.ci.check_repository
```

For a deployed, updated Mission Control service on port 18726, open
`http://127.0.0.1:18726/` and choose a source in the **Development history**
section. Deploy only after the existing Codex/Mission Control session can be
safely restarted: this change should not restart or terminate an active
user-owned coding session.
