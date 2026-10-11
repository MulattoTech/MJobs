# MJobs - public modernization and next product sprint

**Canonical checkout:** `C:\Tools\Dev\MJobs`. **Publication:** GitHub `MulattoTech/MJobs`, `main`. Reconcile live local/remote SHA before writing. Modernization is reviewed in [PR #528](https://github.com/MulattoTech/MJobs/pull/528); its current checks and merge state are authoritative.

## Iteration 13: mock ATS output encoding; comparison integration failure preserved

- One mutation owner under runner10584 lock; immutable AFK goal preserved. Root `C:\Tools\Dev\MJobs`, branch `fix/mock-ats-html-524-20261011`, base/current published main `12466a8cc820712b4842af02c9f2d4482102827a`. Original dirty release-candidate plan preserved and excluded from staging. Prior comparison source remains recoverable on `feat/durable-uploaded-comparison-157-20261011` at `9e1f3231e0c53bf24795f6ed3fce40e8d59dcc5e` / PR #543.
- #524: three reported reflective HTML paths and sibling receipt-confirmation sink repaired through contextual URL/HTML encoding. Persisted receipt identity, controlled fixture review, auth challenge, uploads and exactly-once confirmation remain intact. Thirteen failing regressions reproduced first. Current local validation: 31 focused tests/10 subtests, one HTTP-to-Chromium security test, 34 browser/pipeline tests/2 subtests, and 1,362 unit tests/751 subtests PASS. Fresh local Luna review found no concrete bypass/regression. Catalogue/progress/agent-skills/diff checks PASS. Sanitized source hashes and commands: `docs/evidence/2026-10-11-mock-ats-html-security.json`.
- Security commit publication identity is in AFK queue/events. Hosted security candidate checks NOT_RUN; serial security PR opening waits for #543. No alert dismissed: current main still has 35 high/3 medium open findings. Parent #524 and whole release remain incomplete; goal 0/7, original/reference acceptance unchanged.
- PR #543 required fast CI PASS, but exact PostgreSQL integration FAIL: run `38178346299`, attempt 1, job `114585403128`, `test_durable_admission_replay_and_reopen` asserts `replayed=True` but receives false. Shard 1: 352 PASS/1 FAIL/16 SKIP/89 subtests; other shards PASS. Compose run `38178346211` fails `Mobile overflow`. All four browser shards in run `38178361865` failed: 178 PASS/27 FAIL/15 subtests. Exact redacted job packets retained under ignored `outputs/ci/iteration13-*`; no unchanged rerun.
- Next: finish #543 by tracing PostgreSQL insert/replay metadata, preserve its assertions, verify the changed hypothesis on isolated PostgreSQL and required checks, then protected integration and serial security publication. Real submissions OFF; private data/configuration untouched. Security rollback is a normal source revert; no schema/data change.

## Iteration 11: recover saved application preview and answer isolation acceptance

- Root `C:\Tools\Dev\MJobs`; protected PR #541 merged as `692642ad1e458f5c2d23873a58367ee31d88e97d`. Its source tree matches tested candidate `911e9bc3`. Candidate branch `test/application-preview-532-main-20261011`; exact commit/publication and input hashes live in AFK queue/events and ignored `outputs/ci/iteration11-evidence.json`. One mutation owner, runner10584 lock retained; original dirty release-candidate plan excluded from staging.
- #532: current preview and answer-matching failures reproduced as a semantic selector collision between saved Application review and Multi-page application review. Scope Applications to Main navigation and saved review to its exact region. All original acceptance assertions remain intact; production code unchanged.
- Connected Chromium/API/SQLite regression: 9 browser tests and 2 delayed-response subtests PASS; API integration: 5 tests and 6 subtests PASS. Proven preview reload, missing route/document UNKNOWN blockers, ranked candidate review gating, application/profile isolation, consent/conflict/readback and supporting-document/navigation recovery. Catalogue/progress/dashboard/agent-skills/reference-provenance/diff PASS; local Luna read-only review found no blockers. Browser tests used disposable local configuration; real submission OFF.
- Existing full-browser run38176330301 attempt1 on exact candidate911e9bc3 still runs; shards2/3 FAIL, other shards pending at checkpoint. Exact failed-job logs unavailable until run completes; no unchanged rerun dispatched. Prior baseline167PASS/37FAIL remains historical. Whole release oracle NOT_RUN this iteration; original106/366 and reference19/59 PASS unchanged; goal0/7.
- Next: inspect candidate exact-head required checks and protected integration; collect all four completed browser shards and exact failure packets, then reproduce the next highest-impact data/state defect. No private data/configuration/keys/volumes altered. Rollback via a new test/source revert.

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
