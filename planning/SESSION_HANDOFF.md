# MJobs - public modernization and next product sprint

**Canonical checkout:** `C:\Tools\Dev\MJobs`. **Publication:** GitHub `MulattoTech/MJobs`, `main`. Reconcile live local/remote SHA before writing. Modernization is reviewed in [PR #528](https://github.com/MulattoTech/MJobs/pull/528); its current checks and merge state are authoritative.

## Iteration 7: application restoration navigation repair

- Root `C:\Tools\Dev\MJobs`; base/main/origin main `5bb81009786567f8f001f468da739e55d4f338b5`. PR #537 merged through required checks; integration run 38172311649 passed all three shards and its aggregate. Branch `fix/application-restoration-race-532-20261011`; one mutation owner under the installed AFK runner.
- #532: delayed saved-application restoration no longer overrides explicit navigation. Pending restoration is claimed once, failed reads release the claim for retry, profile changes invalidate it, and startup has one refresh owner. Three fact edit/review/download journeys now execute through durable generated-document checks.
- Deterministic regression: both delayed list/detail responses fail against the original implementation; failed-read retry also fails against the first candidate and passes after review repair. Local Luna review found no remaining blocking findings. Final build and 768 frontend tests PASS; catalogue/progress/dashboard/agent-skills/reference-provenance/diff checks PASS.
- Final browser matrix: 10 passed, 2 subtests passed, 6 failed in 129.37s. Both crash recovery cases passed. Demo readiness, reviewed-answer editor, supporting-document save, two ambiguous preview region selectors and an ambiguous tracker alert selector remain FAIL. Hosted candidate checks await publication. Full four browser shards and whole release gate are NOT_RUN for this candidate; no parent acceptance promotion.
- Original 106/366 PASS and reference 19/59 PASS remain unchanged. Saved goal statement/criteria/verification commands remain intact. Existing dirty RC plan excluded from source staging; legacy worktrees/claims, personal configuration, original keys and populated volumes preserved. Real external applications OFF.
- Next: publish this verified bounded repair via a short-lived PR and native protected auto-merge, then diagnose the highest-value remaining #532 failure from current JUnit evidence. CLI API authentication remains invalid; the existing GCM Git route and connected GitHub tools work. No new supervisor or integration lane.

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
