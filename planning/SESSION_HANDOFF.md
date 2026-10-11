# MJobs - public modernization and next product sprint

**Canonical checkout:** `C:\Tools\Dev\MJobs`. **Publication:** GitHub `MulattoTech/MJobs`, `main`. Reconcile live local/remote SHA before writing. Modernization is reviewed in [PR #528](https://github.com/MulattoTech/MJobs/pull/528); its current checks and merge state are authoritative.

## Iteration 16: active comparison row mobile repair

- Same installed runner10584, single mutation owner, root checkout and PR #543 branch. Parent source `009a85ac20b7d439c43387f606a8246d2f8a1dfe`; publication identity is recorded in AFK queue/events. Goal contract and original dirty release-candidate plan preserved.
- #532 / MJ-REQ-255: table containment did not constrain an active operation; connected browser reproduced437px document width at390px. Operation text now shrinks/wraps and Cancel remains reachable. Regression uses real prepare/upload/queue admission, clicks Cancel, verifies202/CANCELLED and database persistence, reload and profile isolation.
- Final focused Chromium4PASS/0SKIP; frontend768PASS/131files; production build, catalogue, agent-skills, progress/dashboard and diff checks PASS. Local Luna review found malformed seed; corrected via production admission and reverified; follow-up found no important issue. Evidence: `docs/evidence/2026-10-11-activity-operation-mobile.json`.
- Predecessor hosted: integration all3shards/aggregate PASS; Compose run38180343193/attempt1/job114591365134 FAIL mobile overflow; full browser run38180341193 all4shards FAIL. Exact failure packet/log preserved; no unchanged rerun. Corrected-head hosted checks remain pending. Parent requirements and goal0/7 remain incomplete; full local Compose NOT_RUN (Docker unavailable); submissions OFF.
- Next: inspect corrected-head Compose/integration/browser jobs and repair remaining concrete failures before protected integration. Preserved security branch3d7f54b3 waits for serial publication. Rollback via source revert; no data/migration/configuration changes.

## Iteration 15: Activity mobile repair; predecessor PostgreSQL proof passed

- One mutation owner under live runner10584 lock; immutable AFK goal remains0/7. Root `C:\Tools\Dev\MJobs`, branch `feat/durable-uploaded-comparison-157-20261011`, predecessor `b0196fdf519e98ed5e066cc7d6998d9e82a35d59`; remote main `12466a8cc820712b4842af02c9f2d4482102827a`. Original dirty release-candidate plan remains preserved and excluded from staging. Exact publication identity lives in AFK queue/events.
- #157 replay repair: exact-head PostgreSQL run38179775393/attempt1/job114589675384 succeeded. Downloaded JUnit proves all five durable comparison PostgreSQL tests PASS/zero skips; integration shard354PASS/16SKIP/89subtests, all three shards PASS. This proves the predecessor revision, not subsequent bytes.
- #532 / MJ-REQ-255: reproduced Activity page width1669px at viewport390px after a real saved-search operation. The unstyled ten-column history wrapper caused overflow. It now contains horizontal scrolling with a named keyboard-focusable region and visible focus; every column remains available. Connected regression covers ArrowRight, final-column access, durable reload and profile isolation.
- Local: 4 connected Chromium PASS; 768 frontend PASS; 21 history/API PASS and4subtests; production build/catalogue/progress/dashboard/agent-skills/diff checks PASS. Read-only local Luna review found no concrete issues. Source hashes and exact scopes: `docs/evidence/2026-10-11-activity-mobile-overflow.json`. Default TEMP ACL and Vite process permissions were handled through isolated fixtures/approved execution; no security policy relaxed.
- PR #543 remains PARTIAL until the new revision has exact-head hosted checks. Predecessor Compose run38179775366/attempt1/job114589675188 still fails mobile overflow; the repair changes that condition, with no unchanged rerun. Full browser failures remain unresolved. Local Compose NOT_RUN (Docker unavailable). No acceptance authority promoted; real submissions OFF.
- Security branch `fix/mock-ats-html-524-20261011` at `3d7f54b368d96865d45789a9753078a206ee69cb` remains preserved for serial PR publication after543. Next: inspect new-head required/Compose/browser jobs, repair actionable failures and integrate through actual protection; then publish security repair. Rollback through source revert; private configuration/data untouched.

## Iteration 14: comparison replay metadata repair

- One mutation owner under live runner10584 lock; immutable AFK goal preserved. Root `C:\Tools\Dev\MJobs`, branch `feat/durable-uploaded-comparison-157-20261011`, predecessor `9e1f3231e0c53bf24795f6ed3fce40e8d59dcc5e`; live remote main `12466a8cc820712b4842af02c9f2d4482102827a`. Original dirty release-candidate plan preserved and excluded from staging. Publication identity belongs in AFK queue/events.
- #157 / MJ-REQ-156: replay metadata no longer relies on INSERT rowcount; INSERT ON CONFLICT RETURNING identifies actual insertion while existing row lock, immutable input checks and terminal states remain intact. Unknown-driver-count regression failed first on real SQLite writes; now first admission, replay, exactly one persisted operation and completed replay flags pass. Scoped PostgreSQL wrapper includes the new regression.
- Local comparison: 25 PASS/6 subtests; five PostgreSQL tests NOT_RUN (Docker unavailable, no native service found). Controlled connected Chromium: 3 PASS. Full unit suite and build/validators PASS; counts and exact executable hashes in `docs/evidence/2026-10-11-comparison-replay-metadata.json`. Read-only local Luna review found no critical/important issue; owner verified with disposable project-local TEMP after reviewer hit default-temp ACL. Protected Git credential recovered API access without credential changes.
- PR #543 remains PARTIAL; its predecessor PostgreSQL failure is run38178346299/attempt1/job114585403128. Required checks for the repair revision must execute before integration. Compose mobile overflow and all four full-browser shard failures remain unresolved; no unchanged manual rerun. Acceptance authorities and goal0/7 unchanged; real submissions OFF.
- #524 security repair remains preserved on `fix/mock-ats-html-524-20261011` at `3d7f54b368d96865d45789a9753078a206ee69cb`, locally verified and waiting for serial publication after543. No alert dismissed or independent certification claimed.
- Next: inspect exact repair-head PostgreSQL/required checks, then reproduce the Compose mobile-overflow blocker before protected integration. Rollback via source revert; no migration/private configuration/data changes.

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
