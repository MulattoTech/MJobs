# MJobs Codex dashboard

The active workflow uses one Codex integration checkout and the evidence-backed acceptance dashboard, not parallel workers or CLI quotas spread across providers.

For a quick view, open `docs/PROGRESS_DASHBOARD.md` or run `python -m tools.verification.progress_dashboard --summary`. Use `planning/REQUIREMENT_INDEX.json` to find requirements by priority, area and acceptance state. GitHub issues have their own priority, category and area labels.

Launching Codex through Mission Control requires explicit prompt preview and approval. The system must never start a second session to bypass approval, force-control a running process, create extra worktree roots, or treat a successful source commit as automatic acceptance. Historic multi-lane evidence remains recoverable in Git history.
