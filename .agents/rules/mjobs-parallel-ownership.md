---
trigger: always_on
description: "MJobs cross-agent ownership and safe integration constraints for Antigravity."
---

# MJobs Antigravity integration boundary

When asked to implement MJobs features with another live Codex session, read the root `AGENTS.md`, `GEMINI.md`, `docs/DUAL_AGENT_COORDINATION.md`, `planning/lanes/README.md`, and `docs/PROGRESS_TRACKING.md` before edits. The root `main` checkout and canonical `planning/**` are integration-owner-only.

The Antigravity lane is **AI matching**: claim specific new or unclaimed files in `services/matching/**`, matching-specific tests, or self-contained components on an isolated `antigravity/priority-matching-ai-*` worktree. Never modify Codex-owned `services/applications/**` or `workers/applications/**`, shared persistence/migrations, root `main`, application router mounts, the canonical acceptance matrices, or project-level coordination rules. An overlapping live claim is a hard stop for that slice.

First verify real current branch and remote base; check the machine-local claim bus and GitHub issue claim comments; claim files before implementation. Commit only owned implementation, tests and sanitized unique evidence to the Antigravity branch. Report an exact issue, SHA, evidence, test results, and blockers for serial integration. Do not run GitHub-hosted agents/CI, bypass ATS barriers, or submit real applications.
