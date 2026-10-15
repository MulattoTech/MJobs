---
name: mjobs-delivery
description: "Single-workspace Codex delivery skill for MulattoTech/MJobs: inspect authorized issues, safely implement and locally verify meaningful features, and record honest acceptance."
version: 1.0.0
compatibility: Codex Agent Skills SKILL.md
---

# MJobs development (Codex only)

Read the current user's request, `AGENTS.md`, live Git/GitHub state, `planning/REQUIREMENT_INDEX.json`, the affected issue, spec and code. Do not load historical full journals, spawn competing CLI roots or create parallel lane claims. Explicitly user-authorized AFK-DEV iterations may use one mutation owner, fresh context and independently verified completion within normal permission/runtime limits.

Work in `C:\Tools\Dev\MJobs` and publish tested changes through short-lived reviewable PRs and native required checks into GitHub `main`. Native auto-merge is not a human review. Keep existing uncommitted data and historical unmerged work intact unless an explicit, verified backup makes removal safe. The current issue's priority/category/area labels guide the next authorized slice; code and executed tests—not issue closure or merge alone—determine acceptance.

Before each authorized publication, follow `docs/PR_PUBLICATION_POLICY.md`: run `python -m tools.verification.pr_queue --live --candidate <reviewed-evidence.json>` (optionally `--metadata <reviewed-metadata.json>`). The default is 32 concurrently OPEN development PRs, including drafts, with a soft warning; only authenticated dependency automation is excluded. Preserve meaningful commits, owner, local validation and duplicate/empty safeguards even above the soft threshold. Report backlog, CI and conflict uncertainty; stop after the bounded authorized slice. Publication slots do not grant CI capacity or integration permission. Only the integration owner integrates through protected checks and explicit applicable authorization.

Load only the relevant specialized playbook:
- database schema: `references/db-migration.md`
- job source: `references/source-adapter.md`
- application adapter: `references/application-adapter.md`
- security/private facts: `references/privacy-security.md`
- LLM integration: `references/llm-provider.md`
- release evidence: `references/release-verification.md`

When borrowing inspiration, use `docs/reference/REFERENCE_IMPLEMENTATION_POLICY.md` and `python -m tools.verification.reference_provenance`. Never copy unlicensed work, bypass anti-bot/access limits, invent career facts, or submit real applications without explicit user authorization.

For each verified slice: inspect → implement → run affected unit/integration/browser checks → review diff → record exact evidence and remaining gaps → update affected acceptance only → refresh compact index/dashboard at a checkpoint → commit with AI attribution trailers → safely publish. Reviewed public GitHub Actions run exact-head CI and synthetic checks; paid hosted AI reviews remain disabled. Report skipped checks as NOT_RUN; partial parent issues remain PARTIAL.

Local checks include `python -m tools.verification.catalogue`, `python -m tools.verification.progress validate`, `python -m tools.verification.agent_skills`, targeted Python tests, frontend tests/build, and `git diff --check`. See `docs/PROGRESS_TRACKING.md` and `docs/USER_TESTING.md`. At the end keep `planning/SESSION_HANDOFF.md` short.
