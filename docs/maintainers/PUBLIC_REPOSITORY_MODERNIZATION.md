# Public repository modernization — staged execution plan

**Repository:** MulattoTech/MJobs, October 11, 2026. **Status:** implementation plan; individual checkmarks require evidence.

## Guardrails

- Keep a single canonical development history and preserve active Codex sessions, private Compose volumes, credentials and worktrees.
- Never publish user resumes, application records, session cookies, encryption keys, internal logs or real applicant screenshots. CI runs with synthetic test data and read-only tokens on GitHub-hosted ephemeral runners, never on the personal PC.
- GitHub visibility is already PUBLIC. The old `mjobs-history-backup-20261010` release exposes an 8.7 MB pre-consolidation bundle containing 2,183 refs; 11 `archive/mjobs/*` tags point at unmerged history. Audit these and preserve a private offline recovery copy before any deletion. Public copies already downloaded cannot be recalled.
- Do not select an open-source license, rewrite history, delete recovery releases/tags, enable paid AI agents, publish GHCR images, or enable real external job submission without the owner's explicit decision.
- No privileged `pull_request_target` execution of PR code, shared self-hosted runners, or public workflow access to personal secrets.

## Phase 1 — Public identity and safe housekeeping

- Replace old README with actual sanitized fixture screenshots, tested Windows quick start, optional PERSONAL mode, honest scope/limitations, developer guide and relevant links.
- Add `SECURITY.md`, `CONTRIBUTING.md`, PR/issue templates, GitHub labels, and link checks. Preserve the existing specification, evidence and 366+59 acceptance denominators.
- Add a clear project description and discoverability topics. Do not imply the software is already a production-quality auto-applicant.
- Inventory stale instructions; remove current-tree orphan references and redundant guidance only after validating callers. Do not erase historical evidence because it looks old.

## Phase 2 — Efficient, secure continuous integration

| Gate | Trigger | What it proves | Required for integration? |
|---|---|---|---|
| Fast Python/governance | pull_request + main push | unit tests, requirements, catalogue, progress, AI provenance, formatting | after green |
| Fast frontend | pull_request + main push | npm lock install, Vitest, TypeScript, Vite production build | after green |
| Connected Compose | manual; scheduled weekly after baseline | disposable FIXTURE PostgreSQL, Redis, workers, migrations, mock E2E and restart recovery | only once consistently reliable |
| Full browser suite | manual/nightly or selected PR changes | isolated Chromium UI and known broken browser tests | advisory until failures are repaired |
| Codespaces smoke | manual/changes to devcontainer | cloud development bootstrap works | advisory |

Design: Ubuntu standard GitHub-hosted runners, exact dependency manifests, least-privilege `contents: read`, fixed job timeouts, concurrency cancellation, package caching, bounded artifact retention, no applicant data, and no public-source live submission. Preserve every meaningful failure as FAIL, not a fake PASS.

**Current blocking state:** actions repository setting disabled; `ci.yml` and `codespaces.yml` use inert `if: false`. Do not simply toggle existing monolithic tests; publish a safer, split workflow first. Public standard runners have free unlimited minutes but do not remove external billing/capacity constraints.

## Phase 3 — GitHub automation and security

- Secret scanning, push protection and Dependabot security updates: **enabled 2026-10-11**.
- Dependabot grouped weekly updates: npm, Python, Actions, Docker where manifests are supported; limit open PRs, require tests for auto-created changes; never automerge blind major upgrades.
- CodeQL: GitHub-managed default setup for Python, JS/TS and Actions, after Actions is enabled, with High/Critical findings prioritized.
- Dependency review: check introduced vulnerabilities on PRs where supported.
- Configure branch rules **after baseline green**: prohibit history rewriting/deletion; require stable quick checks; keep an explicit human recovery/bypass procedure. Avoid accidentally locking the sole developer out.
- Turn on auto-delete-merged-branches and optional auto-merge only after PR checks are reliable.
- Build a GitHub Project view from priority/category/area for P0/P1, Security, Blocked, Ready to Test, and Shipped. Preserve canonical acceptance in the repository index.
- Optionally enable Discussions, private vulnerability reporting, and Pages after the public documentation and contributor policy are ready.

## Phase 4 — History and workspace hygiene

- **Remote:** one `main` branch. The 12 tags are 11 history-retention pointers and one backup release; **not** 12 published software versions.
- **Local:** 998 registered worktrees and 48 branches at the audit. Classify by path/HEAD, tracked/untracked/ignored changes, open process owners, unique commits, current working directory and size. Prune only verified clean unoccupied worktrees. Preserve ambiguous or dirty work.
- Keep private offline Git bundle recovery. Only after auditing exposure and verifying new private archive should historical *public* backup tags and release assets be considered for deletion.
- Define versioning: initial real pre-release `v0.x.y` when acceptance evidence supports it; maintain a CHANGELOG/release notes and build reproducible, tagged GHCR images only after a full acceptance gate.
- Existing evidence is valuable for auditability; archive old evidence with an index, not mass deletion.

## Phase 5 — Feedback loop and measured completion

1. Developer opens a short-lived PR with an issue reference.
2. Fast hosted checks plus dependency scanning run automatically.
3. Fix failures and rerun only affected checks; require green exact-head checks.
4. Merge safely, auto-delete branch, refresh evidence-backed acceptance only for truly completed criteria.
5. Weekly synthetic Compose/browser checks surface regressions; CodeQL/Dependabot produce actionable security work.
6. On accepted release milestone, publish signed/traceable builds and an accessible user-testing guide.
7. Track mean CI runtime, flaky tests, weekly red builds, release blockers, dependency latency, disk/Actions storage and actual issue acceptance. Tune by evidence rather than more workflows.

**Exit criteria:** independent green Python and web jobs, an authorized synthetic end-to-end run, a README with real screenshots and no broken links, correctly scoped security alerts, recoverable history, and a known set of blockers. Production readiness and 100% acceptance remain separate claims.
