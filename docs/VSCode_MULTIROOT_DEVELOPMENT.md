# VS Code: all local MJobs development in one workspace

Use the versioned launcher at the root of the MJobs Mission Control code
checkout. On MulattoTechBox this is:

    C:\Tools\Dev\MJobs-MissionControl-Launch\Open-MJobs-VSCode.cmd

From PowerShell:

    & 'C:\Tools\Dev\MJobs-MissionControl-Launch\Open-MJobs-VSCode.ps1'

The command rebuilds and opens the generated workspace at:

    C:\Tools\Dev\MJobs-MultiRepo.generated.code-workspace

The next launch refreshes its folder list as new workers arrive. VS Code does
not automatically discover new sibling folders without refreshing the
multi-root workspace list.

## What gets included

- The main MJobs checkout, whose shared Git object database exposes ALL
  local linked worktree branches, including commits not yet pushed.
- Every genuine independent MJobs Git clone under C:\Tools\Dev, whose
  separate Git object database is not visible from the primary main graph.
- The pinned MJobs-Codex-App and MJobs-Antigravity-Matching linked worktrees,
  even when their latest commit is not among the most recent.
- The 25 most recently committed additional linked worktrees so the current
  coding workspaces have their own VS Code Source Control entries.

An MJobs sibling folder is not necessarily an independent clone. The Codex
and Antigravity lanes are linked worktrees sharing main's Git database.
The launcher checks git rev-parse --git-common-dir to distinguish them from
genuine clones, preventing duplicate Source Control folder entries.

ALL registered worktrees are inventoried, even if only some are opened, at:

    C:\Tools\Dev\MJobs-MultiRepo.generated.worktrees.json

This is a local inventory, not a Git commit or hosted synchronization.

## Options

Open more individual worker folders:

    & 'C:\Tools\Dev\MJobs-MissionControl-Launch\Open-MJobs-VSCode.ps1' -RecentWorktrees 50

Regenerate files without opening another VS Code window:

    & 'C:\Tools\Dev\MJobs-MissionControl-Launch\Open-MJobs-VSCode.ps1' -NoLaunch

Reuse the existing VS Code window:

    & 'C:\Tools\Dev\MJobs-MissionControl-Launch\Open-MJobs-VSCode.ps1' -ReuseWindow

Include ALL worktrees only if you deliberately accept the performance
impact. On October 10 there were 949 registered worktrees; opening more
than 900 individual folders may overwhelm Source Control:

    & 'C:\Tools\Dev\MJobs-MissionControl-Launch\Open-MJobs-VSCode.ps1' -AllWorktrees -AllowHugeWorkspace

Optionally install the community Git Graph extension:

    & 'C:\Tools\Dev\MJobs-MissionControl-Launch\Open-MJobs-VSCode.ps1' -InstallGitGraph

Or install it directly with:

    code --install-extension mhutchie.git-graph

VS Code also has a built-in Source Control Graph. The extension is optional,
not necessary for local commit/branch awareness.

## How to inspect the colorful local graph

Open Source Control (Ctrl+Shift+G) in the generated VS Code workspace and
expand Repositories. Select MJobs MAIN or a development lane/independent
clone, then use its Graph view. Enable all-branches visibility when useful.
If Git Graph is installed, run Git Graph: View Git Graph from the Command
Palette and select the appropriate repository.

Local commits are visible before GitHub sync. However, each independent
clone has its own graph. Git cannot safely combine separate clones into a
single true branch ancestry graph, and this launcher never fabricates merges.

Git Source Control shows uncommitted changes in opened folders. The commit
graph shows committed history for the selected repository. Linked worktree
branches are part of the main Git repository even when the actual worktree
folder is not opened individually.

## Performance, privacy and safety

Read-only discovery: no fetch/pull/commit/push/reset/clean/stash/worktree
create, no changes to the integration lock, no agent starts or stops. The
only file writes are the two explicitly named generated files OUTSIDE Git
repositories. The only GUI action is opening VS Code.

Only one git worktree list and one git for-each-ref are required to discover
hundreds of linked worktrees; the script does not run git status 949 times.
It disables automatic Git fetch in the generated VS Code workspace but
preserves local status monitoring and confirm-before-sync behavior.

For a faster VS Code startup use -RecentWorktrees 5 or -RecentWorktrees 10.
Main's graph still has all locally committed shared worktree branch refs.
Only folder-level dirty status for unopened worktrees is omitted.

The portable launcher scripts and documentation are backed up on the
MulattoTech/MJobs private GitHub repository. Generated machine-specific
workspace and registry files are not committed.

## Maintainer verification

    .\.venv\Scripts\python.exe -m unittest tests.unit.test_mjobs_vscode_workspace -v
    .\Open-MJobs-VSCode.ps1 -NoLaunch

Unit tests create temporary local Git repositories and never write to MJobs
main or change currently running coding agents.
