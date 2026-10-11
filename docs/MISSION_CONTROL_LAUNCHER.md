# MJobs Mission Control — Codex-only launcher

Mission Control discovers model IDs from the installed Codex CLI and offers the four short profiles under `prompts/codex/`. The user must review the exact effective prompt and confirm launch. All launches operate in the single checkout `C:\Tools\Dev\MJobs` on `main`; no automatic worktree creation, multi-provider lanes or standing ownership claims.

A stale model catalogue, unapproved prompt, changed selection, or already-running session must be rejected before spawning anything. Session names are UI labels, not arbitrary CLI flags. The launcher cannot auto-push or submit real external job applications.

Codex CLI, VS Code and Codex Desktop may also open the MJobs root directly; use `Open-MJobs-VSCode.ps1` for the one-root VS Code path. See `AGENTS.md` and `planning/SESSION_HANDOFF.md`.
