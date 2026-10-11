# MJobs — one VS Code workspace

Open `C:\Tools\Dev\MJobs` as **one folder**, or run `Open-MJobs-VSCode.ps1`. Git Graph then tracks the single mainline; historical worktrees and clones are no longer part of the default development workspace.

For recovery/audit only, old Git refs and worktree manifests were backed up to `C:\Tools\Dev\MJobs-Reconciliation-Backup-20261010`. Do not open or resurrect those roots during ordinary Codex development. Unmerged and dirty history must be reviewed before deletion or integration.

See `AGENTS.md` for the current Codex-only policy.
