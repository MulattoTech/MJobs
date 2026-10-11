# MJobs Git and GitHub workflow

The authoritative development checkout is `C:\Tools\Dev\MJobs`, publishing to `MulattoTech/MJobs` on `main`. Codex CLI, Codex in VS Code/Desktop, or the explicitly requested ChatGPT Web GitHub connector may contribute. Only **one serial integration path** is active; parallel root agents, exclusive issue holds, and automatic worktree creation are retired.

Before editing: verify local/remote SHAs, working-tree status, open issues, the requirement index, and any unmerged or dirty historical work. An issue has exactly one `priority:P0..P6` plus `category:*` and `area:*`. Prioritize accepted user scope; do not infer "completed" from merged code alone.

Develop a coherent vertical slice and execute the affected local tests. Run reviewed GitHub-hosted read-only CI and synthetic fixture tests; no paid cloud/AI reviewer, unattended product development, live third-party submission, or unsafe force push. Use fast-forward publication only if the remote is unchanged and no unrelated changes are lost. Otherwise reconcile safely.

Every new AI code commit has trailers `AI-Agent`, `AI-Model`, `Requirement`, `Validation`, and `UTC-Date`. Use the actual model when known; use `unknown` rather than guess. Keep credentials/user data out of Git. Evidence for test status goes in sanitized `docs/evidence`; full private logs stay local.

Update only verified MJ-REQ and MJ-REF acceptance statuses; regenerate `planning/REQUIREMENT_INDEX.json` and the dashboard on a meaningful checkpoint. Retired branch/worktree history is recoverable from the machine's 2026-10-10 Git bundle backup. At a stop, leave a concise `planning/SESSION_HANDOFF.md`.
