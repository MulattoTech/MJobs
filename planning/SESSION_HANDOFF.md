# MJobs - public modernization and next product sprint

**Canonical checkout:** `C:\Tools\Dev\MJobs`. **Publication:** GitHub `MulattoTech/MJobs`, `main`. Reconcile live local/remote SHA before writing. Modernization is reviewed in [PR #528](https://github.com/MulattoTech/MJobs/pull/528); its current checks and merge state are authoritative.

## Iteration 9: reviewed-answer browser acceptance repair

- Root `C:\Tools\Dev\MJobs`; current base/main publication `c083d5544f91c9e81478e7ed265a0f7ed0bc667a` from protected PR #539 merge. Branch `fix/reviewed-answer-journey-532-main-20261011`; installed runner retains exclusive owner lock. Original dirty release-candidate plan preserved and excluded from staging.
- #532: repaired obsolete answer-review acceptance to exercise the actual Application answers flow, separate future-reuse consent and explicit bank save. Preserves 409 concurrent-edit rejection, successful versioned readback, concurrent answer retention, profile isolation, sensitive draft exclusion, no application/document/attempt mutation and external submissions OFF. Adjacent review-queue conflict assertion targets the global action alert instead of unrelated component alerts.
- Local focused matrix and exact source evidence are in ignored `outputs/ci/iteration9-answer-final2.xml` and `iteration9-answer-final2.log`. Local Luna read-only review found no blockers; suggested integrity assertions were added. Catalogue/progress/dashboard/agent-skills/reference-provenance/diff checks PASS. Final result and publication are recorded in AFK events/queue.
- PR #539 required checks PASS and merged. Its explicitly dispatched four-shard browser run 38174712409: 167 PASS / 37 FAIL / 12 passing subtests; failures retained with exact run/job/head/attempt packets in `outputs/ci/iteration9-exact-browser-failures.json`. It is not a green release gate. No unchanged rerun dispatched. Whole release oracle remains incomplete; original 106/366 PASS and reference 19/59 PASS unchanged, goal 0/7.
- Next: inspect this candidate's required checks and protected integration, then repair the highest-impact remaining browser failure from the exact four-shard inventory. Priority is genuine data/state defects; semantic locator repairs must preserve their behavioral assertions. No second supervisor or integration lane; private data/configuration/keys/volumes untouched.

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
