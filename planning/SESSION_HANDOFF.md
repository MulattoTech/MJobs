# MJobs - public modernization and next product sprint

**Canonical checkout:** `C:\Tools\Dev\MJobs`. **Publication:** GitHub `MulattoTech/MJobs`, `main`. Reconcile live local/remote SHA before writing. Modernization is reviewed in [PR #528](https://github.com/MulattoTech/MJobs/pull/528); its current checks and merge state are authoritative.

## Iteration 6: provider transport security slice

- Base main/public main: `83b46cc7654e80485de2dd60f798ecb70a8a296d`; branch `fix/provider-secure-transport-20261011`. One mutation owner; root coordinator remains read-only. Seven open dependency PRs are separate work.
- #524 / MJ-REQ-175 (#176): OpenAI-compatible/OpenAI/Azure reject remote HTTP and malformed URL controls before provider I/O; literal loopback HTTP and remote HTTPS still work. Request-level redirect denial prevents prompt replay. Rejected profile settings leave settings/audit unchanged across store reopen.
- Acceptance remains PARTIAL: this slice does not triage every CodeQL alert or satisfy all secret-management/P0/P1 criteria. Original 106/366 PASS and reference 19/59 PASS are unchanged. Goal statement and completion command were preserved; seven obligations plus browser/CI requirements normalized in AFK state.
- Focused checks: 53 tests + 95 subtests PASS; baseline regressions fail against original validators (49 security/API cases), redirect regression fails without request-level denial (6 cases). Local GPT-6.1 Sol Medium review found no blocking issues. Catalogue/progress/dashboard/agent-skills/diff checks PASS. Production build PASS after authorized sandbox escalation for Vite spawn EPERM.
- Full unit suite is running at this checkpoint; record its final result in AFK evidence before publishing. Local full browser/PostgreSQL/Redis and live paid provider calls NOT_RUN. Gate FAIL: 96 unmet requirements; exact-main connected/browser NOT_RUN; private security CLI access 401. Hosted checks for this candidate await publication.
- Pre-existing dirty `docs/superpowers/plans/2026-10-11-release-candidate.md` is preserved and excluded from source staging. Private configuration, original keys, documents and populated volumes were untouched; real external submission OFF. Runner owner lock/history retained; no new background supervisor.
- Next: publish this reviewed branch through a short-lived PR, inspect exact-head hosted jobs and use native protected auto-merge when gates permit; then resume package 1 security triage or package 2 browser reproduction. CLI auth is invalid; connected GitHub tools work. Local source hashes/red logs are in `.git/afk-dev/provider-transport-*`.

## Current development contract

Read `AGENTS.md`, the compact requirement index and the affected issue/spec. Use one mutation owner, short-lived reviewable PRs, required hosted checks and native auto-merge. There is no artificial approving human review, old lane system or expired four-hour project deadline. Explicitly authorized AFK-DEV work retains real account/runtime/approval limits and anti-stagnation safeguards.

The next [plan](../prompts/codex/next-plan.md), [goal](../prompts/codex/next-goal.md) and [main directive](../prompts/codex/next-main.md) are checked in. `Start-CodexSprint.ps1` defaults to dry-run; `-Run` deliberately starts the separately installed AFK-DEV loop. No autonomous product-development loop was started by modernization.

## Modernization and recovery

- Public Project: [MJobs delivery](https://github.com/users/MulattoTech/projects/1), 450 initial issues, priority/category/area/acceptance fields and six focused views. Incremental `tools/maintenance/sync_project.py --apply` uses the existing local `gh` login; no broad personal token was put in repository secrets.
- CI covers independent unit/web/policy gates, PostgreSQL/Redis integration, controlled Compose recovery, full browser shards, dependency review and secret scanning. The static docs and guarded release workflows activate on the reviewed main branch; inspect their actual run results, not just file presence.
- Original MJobs work now has an Apache-2.0 license. Third-party rights remain unchanged. `VERSION` is unreleased `0.1.0-dev`; no production release was invented.
- Two bundles were uploaded privately, downloaded and SHA-256 verified before removal of 12 obsolete public recovery tags and the historical bundle release. Main history was not rewritten. Removing public pointers does not recall downloaded copies.
- Recovery directory: `C:\Tools\Dev\MJobs-Modernization-Backup-20261011`. Private remote: `MulattoTech/MJobs-Archive`, release `recovery-20261011`.
- 924 clean, detached, inactive worktrees were moved intact to private recovery storage and unregistered, reducing 1,000 registrations to 76 at that checkpoint. Dirty, branched, external and ambiguous work remained untouched. Their exact inventory and Git metadata are private; do not erase them to make a count look clean.
- The all-ref pattern scan covered 2,790 commits. All 67 candidates were verified checksums, Git object IDs or explicit test mocks; the narrow fingerprint baseline reran clean. This is not a comprehensive PII/copyright/security certification.

## Acceptance truth and remaining product work

No product requirement was promoted by housekeeping. Original 366: **106 PASS / 111 PARTIAL / 149 NOT_RUN**. P0: **7/26 PASS**; P1: **39/116 PASS**. Reference gaps: **19 PASS / 4 PARTIAL / 36 NOT_RUN**. Full product/release acceptance remains incomplete.

Local modernization baseline: **1,321 unit tests and 684 subtests passed**, **768 frontend tests passed**, production build passed; 12 focused new policy/site tests also passed after hardening. Latest hosted results belong to the exact PR/main revision. The initial hosted integration startup quoting failure was fixed, not ignored.

CodeQL's audit baseline contains **35 high and three medium open findings**, tracked in #524. A successful analysis run does not resolve them. Full browser acceptance also has historical failures. The release/AFK completion gate fails closed while acceptance, current checks or high/critical findings remain unresolved.

Highest-value next work: investigate genuine security paths first; repair restored profile/application navigation and answer-review failures; connect saved-search criteria to discovery; implement durable `UPLOADED_RESUME_COMPARE` using existing workers; complete employer enrollment/enumeration/deduplication and supported ATS preparation/recovery. #157 and #293 remain partial where parent acceptance is unproven.

## Private runtime boundaries

Existing personal configuration, original encryption keys, documents and populated Compose volumes were not altered. The older root `.env` lacks its original encryption key: recover it, never replace it against existing encrypted data. Fresh synthetic CI environments are separate from personal data.

Historical user-test stacks used personal port 18815 and fixture port 18810; verify live state before using them. Their helpers/evidence remain under ignored `outputs/rc-20261011/`. Fresh installation is documented in `docs/INSTALLATION.md`. No real external job submission is authorized.

Preserved unfinished tests include `rc-answer-export-nav-selector-20261011` and `rc-team-onboarding-20261011`; do not merge them blindly or discard their changes. Read the original source checkpoint in Git history when a specific failure requires it, rather than loading old transcripts by default.
