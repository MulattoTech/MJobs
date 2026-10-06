# Integration receipt — 2026-10-06

Remotive global job-source adapter source `8b489a05ec5da67184ff5f0c4bbce517f63116df` and evidence/planning were integrated by non-force fast-forward at synchronized local/GitHub main. Issue #309 (MJ-REQ-017.S08) PASS; parent MJ-REQ-017 remains PARTIAL/OPEN. Evidence `docs/evidence/2026-10-06-remotive-job-source-adapter.json`. Seven focused Remotive integration tests, 36 discovery source tests, 316 unit tests, 62 frontend tests, Vite build, and all repository validators passed. Continue to next dependency-ready slice; hosted verification NOT_RUN, external submission OFF.

---

# Active local-only implementation checkpoint — 2026-10-06

Issue #309 / MJ-REQ-017.S08 Remotive global job-source adapter candidate branch `antigravity/mj-pr-087-remotive-adapter-20261006`, based on `c7656bbecec61e2d037bc57869fad4e1ee8f43d4`. Remotive implemented with public JSON collection, multi-company attribution, search semantics normalization, rate-limit/error handling, and registry integration. Evidence `docs/evidence/2026-10-06-remotive-job-source-adapter.json`. Seven focused integration tests, 36 discovery source tests, 316 unit tests, 62 frontend tests, Vite build, and all repository validators pass. Parent MJ-REQ-017 remains PARTIAL/OPEN. External submission OFF; hosted Actions/reviews NOT_RUN.

---

# Integration receipt — 2026-10-06

Extractive target-job summary tailoring source `d53099715ecda0beec5d6b46ef85dbb1f496738b` and evidence/planning were integrated by non-force fast-forward at synchronized local/GitHub main `bff2acf6ffb446c12653b5e9e6c696d6dac6e3bb`. Issue #96 remains OPEN/PARTIAL after readback: https://github.com/MulattoTech/MJobs/issues/96#issuecomment-6022405440. Evidence `docs/evidence/2026-10-06-target-summary-tailoring.json`. 16 focused Python tests, 316 unit tests, 370 integration tests (1 optional PostgreSQL skip), 2 connected Playwright browser tests, 62 frontend tests, Vite build, and all repository validators passed. Continue to next dependency-ready slice; hosted verification NOT_RUN, external submission OFF.

---

# Active local-only implementation checkpoint — 2026-10-06

Issue #96 / MJ-REQ-095 extractive target-job summary tailoring candidate `d5309972c04062c721ee72986008b04a1f572e03` on branch `codex/req095-summary-adjustment-20261006`, based on `fd2f43b0c04f73edd5ace1f0b4e1fd426c1874ec`. Tailored resumes now select whole approved summary facts that match target skills exactly, preserve the entire summary when no facts match, version template/model contracts, and report omitted summary facts in comparison provenance. Evidence `docs/evidence/2026-10-06-target-summary-tailoring.json`. 16 focused Python tests, 316 unit tests, 370 integration tests (1 optional PostgreSQL skip), 2 connected browser tests, 62 frontend tests/build, and all repository validators pass. Broader factual bullet and summary rephrasing remain incomplete; issue #96 stays OPEN/PARTIAL. External submission OFF; hosted Actions/reviews NOT_RUN.

---

# Integration receipt — 2026-10-06

Mock ATS HTTP cancellation source `f4cf42c34682be815b0306e693fb57f0ee911dca` and evidence/planning were integrated by non-force fast-forward at synchronized local/GitHub main `bb0f4b2384580fa31e1e054e04923d6bc2665107`. Issue #5 remains OPEN/PARTIAL after readback: https://github.com/MulattoTech/MJobs/issues/5#issuecomment-6021535660. Evidence `docs/evidence/2026-10-06-mock-ats-http-cancellation.json`. The full 719-test Python run was PARTIAL due one unrelated Uvicorn teardown assertion that passed alone; 13 mock ATS tests and all 37 browser tests passed. Continue to the next local-only P0 safety slice; hosted verification NOT_RUN, external submission OFF.

---

# Active local-only implementation checkpoint — 2026-10-06

Issue #5 / MJ-REQ-004 mock ATS HTTP cancellation candidate `f4cf42c34682be815b0306e693fb57f0ee911dca` on `codex/req004-mock-ats-http-cancellation-20261006`, based on `7f728d4d046dda7f3a53fe914125a21f068b7b74`. Worker cancellation/deadline now reaches questionnaire and receipt GETs; a cutoff after the SUBMITTING reservation preserves `SUBMISSION_UNCONFIRMED` without blind retry. Evidence `docs/evidence/2026-10-06-mock-ats-http-cancellation.json`. Thirteen mock ATS tests, two mock client cancellation/bounds tests, all 37 browser tests, and build pass. Full Python suite ran 719 with one unrelated application-pipeline server-thread teardown failure and one optional PostgreSQL skip; the failing browser test passed alone. Repository validators remain to run before integration. External submission OFF; hosted Actions/reviews NOT_RUN.

---

# Integration receipt — 2026-10-06

Cancellation acknowledgement truthfulness source `6abd58aec2562fddaff1367fa8c60417d98b5ebb` and evidence/planning integrated by non-force fast-forward at synchronized local/GitHub main `19ce41de0c9c380e1dbd3b6278870477c484bd19`. Issue #5 remains OPEN/PARTIAL after readback: https://github.com/MulattoTech/MJobs/issues/5#issuecomment-6021034562. Evidence `docs/evidence/2026-10-06-comparison-cancel-acknowledgement.json`. Continue the next highest-value local-only operation-boundary slice; hosted checks/reviews remain NOT_RUN and external submission OFF.

---

# Active local-only implementation checkpoint — 2026-10-06

Issue #5 / MJ-REQ-004 cancellation acknowledgement truthfulness: source candidate `6abd58aec2562fddaff1367fa8c60417d98b5ebb` on `codex/req004-cancel-acknowledgement-20261006`, based on `2f3e386b5710b2d870b03f115ef20714e88cc7b1`. A `cancelled=false` response now leaves the comparison fetch active and reports that cancellation is unconfirmed; confirmed cancellation still aborts and stops extraction. Evidence `docs/evidence/2026-10-06-comparison-cancel-acknowledgement.json`. Connected browser positive/negative acceptance, full Python 718 (one optional PostgreSQL skip), 62 frontend tests, build and diff check pass. Planning validators remain to run before integration. Cross-replica cancellation and other operation categories remain incomplete; issue #5 stays OPEN/PARTIAL. Hosted checks/reviews NOT_RUN, external submission OFF.

---

# Integration receipt — 2026-10-06

Uploaded-original comparison cancellation source `8a469b03bb7c57448118be9e1b761d8608d179ab` plus evidence/planning integrated by non-force fast-forward at synchronized local/GitHub main `5cdd3885fdf88140aac3bfa74108dd91a10c7cd7`. Issue #5 read back OPEN/PARTIAL: https://github.com/MulattoTech/MJobs/issues/5#issuecomment-6020769000. Evidence `docs/evidence/2026-10-06-uploaded-comparison-cancellation.json`. Continue to the next dependency-ready local-only issue/slice; hosted Actions/reviews stay NOT_RUN and external submission OFF.

---

# Active local-only implementation checkpoint — 2026-10-06

Current synchronized main at checkpoint start: `6cd72c02d930b23fd09b2657f823ae3b18e04629`. Active candidate source commit: `8a469b03bb7c57448118be9e1b761d8608d179ab` on `codex/req004-upload-compare-cancel-20261006`. Uploaded-original resume comparison now has a user-visible cancellation action scoped to profile, application, uploaded revision, and tailored document; extraction cancellation leaves the profile and application document history unchanged. Evidence: `docs/evidence/2026-10-06-uploaded-comparison-cancellation.json`. Focused integration (6) and Chromium (1), full Python (718; one optional PostgreSQL skip), frontend (62), build, and local validators pass. Hosted checks/reviews remain NOT_RUN; external submission remains OFF. MJ-REQ-004 and issue #5 remain PARTIAL/OPEN, with cross-replica signaling and remaining operation/provider classes unverified. Next: verify exact remote main and policy, integrate via safe non-force fast-forward, read back #5 OPEN, then continue the next dependency-ready P0 issue.

---

# Active local-only implementation checkpoint — 2026-10-06

P0 issue #8 / MJ-REQ-007 scoped atomic `.env` initialization is synchronized on local/GitHub `main` at `494e15f46a93b70530755811888ece6046dc98aa`; issue #8 remains OPEN/PARTIAL after https://github.com/MulattoTech/MJobs/issues/8#issuecomment-6019785804.

Active writer: P0 issue #9 / MJ-REQ-008, branch `codex/req008-crash-resume-20261006`, based on `e00ddd9af6f45cd2560cb418055d8a92011e87aa`. The existing resume CLI was exercised inside task-owned container `mjobs-req008-restart-20261006`: network disabled, root and checkout read-only, SIGKILL, restart, and identical checkpoint afterward. The container is stopped; PERSONAL stack stayed running. Seven local progress tests pass. Evidence `docs/evidence/2026-10-06-progress-container-crash-recovery.json`. Issue #9 remains OPEN/PARTIAL because host reboot is unauthorized and browser/product-worker crash classes remain unverified. External submission OFF; hosted checks/reviews NOT_RUN.

---

# Active local-only implementation checkpoint — 2026-10-06

Integration receipt: P0 issue #8 / MJ-REQ-007 code, evidence and planning are integrated at synchronized local/GitHub `main` `494e15f46a93b70530755811888ece6046dc98aa`. Thirteen initializer/local-backup tests and repository validators pass; issue #8 remains OPEN/PARTIAL after https://github.com/MulattoTech/MJobs/issues/8#issuecomment-6019785804 because actual protected-configuration schema migration recovery remains unverified. Evidence: `docs/evidence/2026-10-06-atomic-env-initialization.json`.

Next sole writer: P0 issue #9 / MJ-REQ-008, branch `codex/req008-crash-resume-20261006`, based on `494e15f46a93b70530755811888ece6046dc98aa`. Existing resume CLI and separate-process adoption pass. Reconcile local Docker and exercise a task-owned synthetic container restart; host reboot is not authorized. Preserve any unsupported host-restart class as PARTIAL. External submission OFF; hosted Actions/reviews NOT_RUN.

---

# Active local-only implementation checkpoint — 2026-10-06

Issue #96 / MJ-REQ-095 responsibility-bullet emphasis integrated at `a363ebd686c9237c81bb8c60dc51cb2ba34f77a1`; issue remains OPEN/PARTIAL after GitHub readback https://github.com/MulattoTech/MJobs/issues/96#issuecomment-6019614794. The slice has 26 focused Python tests, 62 frontend tests, production build and synthetic Chromium acceptance; evidence `docs/evidence/2026-10-06-resume-bullet-emphasis.json`.

Active writer: P0 issue #8 / MJ-REQ-007, branch `codex/req007-config-recovery-20261006`, candidate `c7306774884ad08e5172bfec7ab365dc261f77b3` based on synchronized main `a363ebd686c9237c81bb8c60dc51cb2ba34f77a1`. The private `.env` initializer now stages and flushes before atomic create-if-absent publication; 13 local unit/backup tests and repository validators pass. Evidence `docs/evidence/2026-10-06-atomic-env-initialization.json`. The slice does not prove recovery for a protected-configuration schema migration, so #8 stays OPEN/PARTIAL. Next after safe integration: P0 issue #9. External submission OFF; hosted checks/reviews NOT_RUN.

---

# Active local-only implementation checkpoint — 2026-10-06

At this checkpoint, GitHub/local `main` are synchronized at `41abcfb127ed68b7cec656ee92e29643ffc4a877`. Actions remain disabled, no PRs are open, and `main` is unprotected. Issue #10 / MJ-REQ-009 is CLOSED after full local staged-operation evidence/readback at https://github.com/MulattoTech/MJobs/issues/10#issuecomment-6018916813. Issue #8 / MJ-REQ-007 and #9 / MJ-REQ-008 remain OPEN/PARTIAL; P0 external/host recovery blockers remain explicit.

Active writer: issue #96 / MJ-REQ-095, branch `codex/req095-extractive-summary-20261006`, worktree `.worktrees/req095-extractive-summary-20261006`, based on `41abcfb127ed68b7cec656ee92e29643ffc4a877`. Added versioned deterministic emphasis for approved responsibility facts containing exact target-job skill matches; exact fact wording, profile snapshots and older template/model semantics remain preserved. Local unit/integration/history/browser tests, 62 frontend tests and build pass. Evidence: `docs/evidence/2026-10-06-resume-bullet-emphasis.json`. Issue #96 remains OPEN/PARTIAL: summary adjustment and factual rephrasing remain incomplete. Continue the same tailoring requirement with a safety-preserving slice, then resume the dependency-ready queue. External submission remains OFF; hosted checks/reviews NOT_RUN.

---

# Active local-only implementation checkpoint — 2026-10-06

Current main before this evidence checkpoint: `cbaf2323a24fd57798703a700b879869c3731365`. Actions remain disabled, no PRs are open, and main is unprotected. Issues #8 / MJ-REQ-007 and #9 / MJ-REQ-008 remain OPEN/PARTIAL for configuration migration recovery and physical/container interruption proof.

Issue #10 / MJ-REQ-009 staged local acceptance now PASS at exact source `cbaf2323a24fd57798703a700b879869c3731365`. Thirty unit/integration/browser tests passed; isolated fixture Compose built and reached readiness; a controlled public Greenhouse read returned 19 jobs with external submission OFF and zero application requests; synthetic Chromium dry run stopped before submit. Only the task-created fixture project was stopped; the existing PERSONAL stack stayed running. Evidence: `docs/evidence/2026-10-06-staged-local-operation.json`. Real application submission remains optional/OFF; hosted/cloud release remains NOT_RUN. Close issue #10 only after exact GitHub readback, then continue to the next dependency-ready P0 issue.

---

# Active local-only implementation checkpoint — 2026-10-06

Current main before this evidence/code checkpoint: `36b4dbdcefd3a30bbe53f1a523f33bd340c8c76f`; Actions remain disabled, there are no open PRs, and main is unprotected. Issue #8 / MJ-REQ-007 is OPEN/PARTIAL after evidence readback; protected local configuration migration recovery remains unverified.

Current slice: P0 issue #9 / MJ-REQ-008. Added read-only `python -m tools.verification.progress resume`, which validates and emits the active issue, latest milestone, remaining checks and safe checkpoint. A synthetic second process now reloads and adopts a future milestone. Seven focused tests plus progress/catalogue/agent-skill validators pass. Physical host/container/browser/worker interruption was not induced, so #9 remains OPEN/PARTIAL. Evidence: `docs/evidence/2026-10-06-progress-resume-cli.json`. External submission remains OFF; hosted checks/reviews remain NOT_RUN under local-only policy.

---

# Active local-only implementation checkpoint — 2026-10-06

Current main before this planning/evidence checkpoint: `0082a651ae7a5aa5a7be4c5dfe593b3bca61815e`; Actions are disabled, there are no open PRs, and `main` is unprotected. Issue #5 / MJ-REQ-004 remains OPEN/PARTIAL after several bounded public/fixture transport slices.

Current slice: P0 issue #8 / MJ-REQ-007 source control and change safety. Exact-main local backup/integrity and SQLite migration checks passed (22 tests); conditional no-Git manifest is NOT_APPLICABLE because the checkout is healthy Git. Database/document archive restore has prior isolated FIXTURE/PERSONAL receipts. Protected local configuration migration recovery remains unverified, so #8 stays OPEN/PARTIAL. Evidence: `docs/evidence/2026-10-06-source-change-safety.json`. The next lane should reconcile the open P0 progress-journal requirement #9 before other P0 and core workflow slices. Hosted checks/reviews remain NOT_RUN by user policy; external submission remains OFF.

---

# Active local-only implementation checkpoint — 2026-10-06T13:36Z

The current authorization remains sustained local-only issue implementation from synchronized `main`, one writer/integration lane, local deterministic tests/browser/database checks, Git operations and issue updates. GitHub Actions/hosted review remain disabled; external application submission stays OFF.

Current writer: issue #3 / MJ-REQ-002, branch `codex/req002-no-gpu-20261006`, based on synchronized main `593569477dbcf035478940c3275380fd732a7127`. A deterministic CPU-only dependency/runtime validator checks Python/JavaScript package locks, Compose/Docker GPU wiring, and frontend WebGL/WebGPU use; six positive/negative fixture tests pass. The live repository validator passes, Docker Compose parses with a process-local synthetic password placeholder, 62 frontend tests/build pass, 13 connected headless browser tests pass, and the full Python/integration/browser suite passes 696 tests with one optional PostgreSQL skip. Repository skill/catalogue/progress validators and diff checks pass; evidence is `docs/evidence/2026-10-06-gpu-independence.json`. Requirement #3 is PASS, integrated at `0a0c9889ae0b661a872d8f0558c7fdebafbccdfe`, and issue #3 CLOSED after comment/readback https://github.com/MulattoTech/MJobs/issues/3#issuecomment-6016561890. No host hardware/configuration was queried or changed. The next sole writer is P0 issue #2 / MJ-REQ-001: audit the checked-in operational surface for host-safety regressions and add a locally testable guard if one fits the existing architecture.

Issue #3 / MJ-REQ-002 is PASS, integrated at `0a0c9889ae0b661a872d8f0558c7fdebafbccdfe`, and CLOSED after comment/readback https://github.com/MulattoTech/MJobs/issues/3#issuecomment-6016561890. Evidence: `docs/evidence/2026-10-06-gpu-independence.json`.

Current writer: issue #2 / MJ-REQ-001, branch `codex/req001-host-safety-20261006`, based on main `08a6cc59f02bdec252a71c01452f1964fc87990e`. Added a deterministic host-safety validator over checked-in PowerShell/shell/batch/package setup entrypoints, with five positive/negative tests; the live entrypoint scan passes. A high-severity transitive development dependency advisory in `source-map-js` was fixed by a one-package lockfile-only bump from 1.2.1 to 1.2.2; full and production npm audit now report zero vulnerabilities. Web build and 62 tests pass. Full Python discovery ran 701 tests with one optional PostgreSQL skip; one known CompanyDiscoveryRequestsBrowserTests Uvicorn-thread shutdown assertion failed. That case passed alone in 2.9 seconds and all 36 browser tests passed on the same candidate; no timeout was weakened. Evidence: `docs/evidence/2026-10-06-protected-host-safety.json`. Candidate evidence was integrated by non-force fast-forward at `d726c91423343cb6285973b019dec0a7c98a2692`; issue #2 CLOSED after readback https://github.com/MulattoTech/MJobs/issues/2#issuecomment-6017177918. No host hardware/configuration was queried or changed. Hosted Actions/reviews remain NOT_RUN, and external submission remains OFF.

Issue #4 / MJ-REQ-003 headless acceptance is PARTIAL with evidence `docs/evidence/2026-10-06-headless-browser-acceptance.json`: 36 headless browser tests cover local/mock navigation, source/DOM review, upload, multi-step application, validation, synthetic submission confirmation and intervention. No real ATS provider/account exists to verify authentication persistence, so issue #4 stays OPEN; Actions remain disabled and external submission stays OFF.

Issue #4 / MJ-REQ-003 headless acceptance is recorded as PARTIAL in `docs/evidence/2026-10-06-headless-browser-acceptance.json`. Issue #4 remains OPEN because credential-backed ATS authentication persistence requires a real provider/account that is not available; external submission remains OFF.

Issue #4 evidence was integrated at `7e36a1f92959026585ce265e36bc85c8cb521ed8`; the GitHub comment/readback leaves it OPEN/PARTIAL because authenticated real ATS session persistence is BLOCKED_EXTERNAL (https://github.com/MulattoTech/MJobs/issues/4#issuecomment-6017238071).

Current issue #5 / MJ-REQ-004 slice: public DNS resolution polls the durable worker cancellation/deadline callback during pending resolver work; the absolute DNS timeout and caller exception identity remain intact. Candidate `e830589b32f268427404aee8bcac4f55b3a1b4e1` has 21 focused unit, 23 discovery/monitoring integration, two connected headless browser, and 703 full Python tests PASS with one optional PostgreSQL skip. Evidence: `docs/evidence/2026-10-06-public-dns-cancellation.json`. Evidence commit and safe integration pending; issue #5 remains OPEN/PARTIAL because AI provider and real ATS operation classes are unavailable and other boundaries remain. Hosted Actions/reviews NOT_RUN; external submission OFF.

Continue issue #5 by auditing the next highest-value locally testable operation boundary after integrating this slice.

The previous integrated issue #5 cancellation slice is source `e57c1759cd75dd453bbe0284325aee3d3174eb1d` and evidence/planning receipt `f9649d9ae374a31203d6cc2774d7ac47f4e4376b`; local and origin main matched at integration. GitHub issue #5 comment/readback remains OPEN (https://github.com/MulattoTech/MJobs/issues/5#issuecomment-6015630443). Evidence: `docs/evidence/2026-10-06-resume-extraction-cancellation.json`. The slice adds profile/resume-scoped **Cancel extraction**, propagates cancellation through native PDF/DOCX and active OCR child processes, and kills/reaps children while keeping operation deadlines and private diagnostics bounded.

Verification: 56 focused extraction/API/browser tests; 36 browser tests; 688 full Python tests PASS with one optional PostgreSQL skip; 62 frontend tests/build; local skill/catalogue/progress/diff validators; isolated no-network Poppler/Tesseract API/Chromium acceptance and actual OCR child cancellation PASS. One initial full-suite `CompanyDiscoveryRequestsBrowserTests` shutdown assertion passed alone, in all browser tests and same-source full rerun.

Broad MJ-REQ-004 and issue #5 remain PARTIAL/OPEN; real ATS/AI provider paths and complete all-category timeout/output/retry/error policy remain open. Requirement/reference denominators remain 366/59. Continue auditing the next highest-value locally implementable operation boundary.

Historical entries below are preserved and never override live Git/GitHub state.

---

# Active local-only implementation checkpoint — 2026-10-06

The current user authorized continuous issue implementation from current `main`, one writer/integration lane, local validation, GitHub issue updates, commits, pushes, and safe local fast-forwards. Keep Actions, hosted tests/reviews, cloud agents, extra AI sessions, Codespaces, billing changes, and external application submission OFF.

Latest checkpoint: issue #407 / MJ-REF-037 is CLOSED after complete local acceptance and non-force fast-forward integration at `70e16d0766f87c7b637cd4731a7e251ddc47d54a`; all five GitHub criteria were checked, evidence commented and state read back. Evidence: `docs/evidence/2026-10-06-reference-provenance.json`. Nineteen focused tests and provenance, skill, catalogue, progress, and diff validators passed. Hosted Actions/reviews and legal approval are not claimed; external submission is OFF; original requirements remain unchanged and full release remains NOT_RUN. The next sole writer is P1 issue #383 / MJ-REF-013, a bounded ToolRegistry and proposal-only side-effect safety gate; inspect its acceptance and continue without waiting for a prompt.

Issue #387 / MJ-REF-017 is closed after local PASS on candidate `4bb1931f9dc62a312c8f703de24704a3f8550f9e` and non-force fast-forward integration at `6f61044e158336385e3ac481a21740f444db8cde`. All five issue criteria are checked and evidence is in `docs/evidence/2026-10-06-artifact-recovery.json`. It covers crash-safe generated revisions, protected inventory, staging/temp classification, quarantine, restore, purge, and browser history preservation. PostgreSQL/Docker/hosted and release acceptance remain NOT_RUN; automatic retention remains PARTIAL.

Issue #380 / MJ-REF-010 is also CLOSED with all five criteria evidenced on integrated main `8b49fd7e196b8913cae8937b1a50867b72c00c57`; its 597-test local receipt is `docs/evidence/2026-10-06-claim-preservation.json`. The original capability denominator remains unchanged.

Issue #380 / MJ-REF-010 now has local implementation candidate `c37d0908caf0a78095befafa6ea7e53e3ddc515c` on `codex/mj-ref-010-claim-guard-20261006`, based on `7eb2463d115f32af65e66263f257c48e0b54d77`. Full Python unit/integration/browser discovery passed 597 tests; focused claim and adjacent browser suites, frontend build, and repository validators passed. Evidence: `docs/evidence/2026-10-06-claim-preservation.json`. It remains OPEN until candidate integration and complete acceptance readback. Reconcile current Actions/protection/ancestry, push and non-force fast-forward only if safe; then close only with all five criteria evidenced. Immediately continue to #373 / MJ-REF-003 and other ready safety/privacy prerequisites. Do not modify the separate Codespaces checkout or private runtime/data. Keep broader requirements and full release status separate.

---

# Guided hands-on review tooling ? 2026-10-05

The next user-directed product phase is a manual VS Code/Luna walkthrough, not an autonomous implementation run. docs/GUIDED_PRODUCT_REVIEW.md, Start-GuidedReview.ps1, and the two Guided Product Review VS Code tasks launch PERSONAL or FIXTURE review with the existing private feedback workstation. The new 20-core-workflow-review task records one finding at a time.

Keep phases separate: prompts/VS_CODE_GUIDED_REVIEW.md facilitates without source/GitHub mutation; a fresh prompts/FEEDBACK_TO_GITHUB_ISSUES.md turn creates or updates sanitized dependency-aware issues; later implementation uses prompts/FIX_FEEDBACK_ISSUES.md plus the local-only budget policy. GitHub Actions/cloud reviewers remain disabled. External submission remains OFF. No product requirement status changes merely because this review tooling exists.

---

# Current local-only reconciliation — 2026-10-05

The current user stopped GitHub-hosted agents, reviews, automated tests and Actions.
Repository Actions and all three known workflows are disabled. Current AGENTS.md
and docs/GITHUB_WORKFLOW.md supersede older hosted gates and the expired afternoon
trial deadline. Do not attempt billing recovery or restart an old agent session.

PR515 at source bbc340221677041d0c2e4e21dc15862e155cfb6b contains the existing
connected synthetic A-F test, planning and workflow changes, not new product code.
Main at reconciliation start was 3ec8702ed0f42842e1bac772997760d8db5c9caf.
This session freshly passed 62 frontend tests, the production build, and the combined
Chromium/API/temporary-SQLite journey using synthetic data. No new hosted run or
independent reviewer was requested. Docker Linux is unavailable; full release and
private-owner testing remain NOT_RUN. The source was integrated by an ordinary non-force fast-forward at
`ae7e27a6202272ab5a65408df91a3ccaff151dfd`. PR515 is closed and GitHub
reports its head integrated. Its closing receipt records the exact final metadata
SHA and local checkout synchronization; do not retry the old billing-blocked runs.

Already implemented: manual capture, approved-fact matching explanations,
deterministic job-specific cover letters, reviewed reusable answers/exports,
job dispositions/notes and user-reported application pipeline/history. Preserve the
profile ownership, strict save-version and review-reset fixes. Source/provider/AI,
broader tailoring, answer policy and other full contracts still have open gaps.

All 366 original requirement results remain unchanged: 64 PASS, 76 PARTIAL,
226 NOT_RUN. Additive references and full release readiness remain separate.
Matrix-versus-issue disagreements are listed in the reconciliation evidence;
no issue was closed just to make the totals agree.

No implementation PR remains active for this handoff. Next invocation:
`prompts/LOCAL_ONLY_LUNA_ITERATION.md` with GPT-6 Luna High.
Use one local writer, ordinary explicit local validation and frequent safe Git
checkpoints. No extra model sessions, hosted automation or private-data uploads.
The user's raw docs/CLI_Output transcript is preserved locally and newly ignored.
The separate Codespaces checkout and all retained private runtime data are preserved.
No application deployment, live migration or real submission was performed.

Evidence: docs/evidence/2026-10-05-local-only-reconciliation.json.

---
## Historical handoff snapshots (not current execution authority)

## Final stabilization checkpoint — 2026-10-05T18:57:31Z

PR [#515](https://github.com/MulattoTech/MJobs/pull/515) remains draft at recorded checkpoint `7151348d0b92cd9cb23784785190e0d17ab892b1` against `3ec8702ed0f42842e1bac772997760d8db5c9caf`. The current-state metadata review passed. Exact push Verification run [37359494332](https://github.com/MulattoTech/MJobs/actions/runs/37359494332) and pull_request Verification run [37359772249](https://github.com/MulattoTech/MJobs/actions/runs/37359772249) both failed before starting because GitHub reported recent account payments failed or the spending limit needs to increase. No workflow steps, runtime reports, artifacts, or merge-tree result were produced. Do not merge; keep the source synchronized on the draft branch until GitHub Actions access is restored, then rerun both event types and inspect all required artifacts.

Four consecutive local connected Chromium runs pass on the corrected after-reload capture readback; progress, catalogue, and agent-skill validators pass. Earlier bounded Greenhouse GET evidence is separate and not final-head hosted proof. A-F remains PARTIAL, product AI unavailable, external submission OFF, and the retained private runtime/database untouched.

## Latest stabilization checkpoint — 2026-10-05

PR [#515](https://github.com/MulattoTech/MJobs/pull/515) remains draft on `codex/core-workflow-trial-combined-journey-20261005`. Independent review of published head `4548eab` found the manual capture detail API assertions happened before browser reload despite the evidence claim, and stale progress summaries still described title/location criteria as persisted and c069798 as current. The readback has been moved after reload; the capability and progress summaries now distinguish saved scheduled-search title/employment criteria from transient Jobs title/location filters and label prior candidate evidence as historical. Four consecutive local browser runs pass. The repair is published at checkpoint `c5799d6`; independent review confirmed the oracle and capability wording but found this handoff/evidence still described the repair as awaiting publication. That wording correction is included in published checkpoint `03d39ee`; it still needs fresh review and both exact-head Verification events.

The in-progress push and pull_request Verification runs `37357784353` and `37357791015` target superseded `4548eab`; neither can qualify a repaired head. Keep PR #515 draft until independent review, both events, required runtime reports/artifacts, and PR merge-tree equality pass. Overall A-F remains PARTIAL; product AI is unavailable/not implemented, external submission remains OFF, and the retained private runtime/database remains untouched.

Earlier bounded live Greenhouse read: push run `37352733344` on source `b62185e8` returned 19 public jobs, zero external application requests, and safe UI readback; this is not final-head CI evidence.

# Historical CORE-WORKFLOW-TRIAL checkpoint — superseded 2026-10-05

At this historical checkpoint, main was `3ec8702ed0f42842e1bac772997760d8db5c9caf` and PR #515 candidate was `c06979890777c1d36b0e49cc9c684591af16adc1`; later review findings superseded that candidate and its no-finding review. Current base includes merged PR #514 CI deduplication, so this branch merges current main and adds a narrow push-trigger exception for `codex/core-workflow-trial-*` to meet the user's explicit both-event gate. Ordinary feature pushes stay deduplicated.

At the historical c069798 checkpoint, the combined synthetic Chromium journey passed four local runs. It covered deliberate resume-fact review, saved scheduled-search title/employment readback plus transient Jobs filtering, safe manual job capture, explainable approved/missing skill evidence, job-specific documents with immutable earlier bytes, exact reviewed answer export, manual reported application status/notes, and database/API/browser reopen. Synthetic only; external submission OFF. A separate exact push run on earlier product-equivalent source `b62185e8ff94059c50c2976e2daf7218fbb38d18` passed all nine runtime reports, including a bounded actual Greenhouse read (19 jobs, zero external application requests) and UI policy readback; artifact `11362759827` digest `a02f0cb5d28397d13c6971cfb4dfbb5946762c59ab2313e3d9b4325834f204c7`. A reviewer found no actionable findings at that historical checkpoint; subsequent exact-head reviews found evidence/oracle issues that were repaired later.

Final metadata/evidence checkpoint is in progress. Current exact-head push and pull_request runs must both pass on the final commit; inspect every required report/artifact and merge-tree equality, then merge only that expected head. If a gate fails, keep #515 draft and report PARTIAL. Product AI is unavailable/not implemented. Full requirement acceptance remains separate and PARTIAL. Do not start/update the retained private runtime or database. The user confirmed GPT-6 Luna High and released the old client; this interface cannot independently inspect billing metadata.

See `docs/evidence/2026-10-05-core-workflow-trial-combined.json` for local synthetic evidence and limitations. The closing PR receipt is the final location for exact run IDs, artifact digests, candidate/base/merge SHAs, avoiding a commit that claims to verify itself.

---

# Active CORE-WORKFLOW-TRIAL checkpoint — 2026-10-05, 11:58 America/Chicago

GitHub `main` is still `2de5863da7418bf20087822d052b2beeb77ca64b`, including answer-bank PR #511 and job-tracking PR #512. PR #513 is open as a draft on `codex/core-workflow-trial-application-pipeline-20261005`, with source head `8756881027674346bd022716068b4532cbc91ca7` and base `2de5863da7418bf20087822d052b2beeb77ca64b`. The exact source head is remotely synchronized and independently reviewed with no actionable code defects.

The F2 pipeline uses the existing append-only audit history for profile-scoped user-reported stages and notes; “Applied” remains separate from worker state and confirmation. Privacy review also moved resume comparison, application detail/history, document downloads and fixture mock actions behind profile-scoped ownership checks. Old unscoped read routes are retired. Synthetic local evidence on source head 8756881: 585 Python tests in 323.820s, 42 focused integrations, 5 Chromium browser tests, 62 web tests, strict build, CLI compilation and repository validators all pass. One earlier run on superseded 7c07db2 had a browser server-thread teardown timeout; the test passed alone and in the 585-test rerun.

Checkpoint c245b9b2002259a998838619284ffaade1ca7207 is published and independently reviewed. It fixes the stale placeholder, completed commit/push wording, incorrect 37-versus-42 count, and stale active-check label. Push run 37343371425 and pull_request run 37343377185 passed on c245b9b, including merge-tree equality; both are superseded by the current sanitized E receipt/state correction. The correction records PR #511 exact-head merge and its nine passing reports plus matching artifact digests, and refreshes the active journal fields. Read back the resulting PR head/base and remote SHA, then obtain fresh independent review and both event-specific checks with all required reports/artifacts and merge-tree equality. Keep PR #513 draft and issues #196/#240 open. Combined A-F browser proof and full requirements remain PARTIAL. Continue after safe merge only before the 13:00 America/Chicago feature cutoff; then stabilize. External submission is OFF, private applicant data was not used, the retained personal runtime/database remains untouched, and the separate Codespaces checkout is preserved. See `docs/evidence/2026-10-05-application-pipeline.json`.

---

## Previous answer-bank checkpoint — 2026-10-05, 06:50 America/Chicago

GitHub main is `fb93ad48a3987c375350388ba8bf677db58575d2`. PR #511 is a draft on `codex/core-workflow-trial-answers-20261005`. The bounded E slice has question-aware answer snapshots and approval resets, exact-normalized suggestions with deliberate per-job review, scoped history/downloads, and immutable SHA-256 TXT exports. Independent review of candidate `315c2e1` found a delayed old API client without `expected_version` could restore an answer approval after a newer revocation. The current uncommitted repair requires that token for every existing-profile API update, returns 409 on missing/stale versions, and retains the transactional compare-and-swap for overlapping saves. The current full local suite passes 567 tests in 339.768 seconds; fresh focused browser/API flows, 62 web tests and build pass. Candidate `315c2e1` push and pull_request events (`37303429620`, `37303434314`) both passed, including required reports, but are superseded because of the independent review finding. The earlier d98 push failure (`37301337462`) was fixed by allowing 15 seconds for the same DRY_RUN_COMPLETE state while retaining STOP_BEFORE_SUBMIT assertions.

The strict-version repair is locally verified; source/evidence checkpoint still needs commit and push. Obtain a fresh independent exact-SHA review and both exact-head Verification events with all runtime reports before merge. Keep #511 draft until then. MJ-REQ-103..107 remain PARTIAL; exact-only matching, fixed confidence floor, sensitive/legal policy and broader requirements remain open. Original MJ-PR-028/029 dependencies remain intact. External submission is OFF; no private applicant data, retained runtime or product AI provider was used.

# Active CORE-WORKFLOW-TRIAL checkpoint — 2026-10-05

GitHub main is fb93ad48a3987c375350388ba8bf677db58575d2. PR #510 delivered the deterministic job-specific cover-letter slice and merged from candidate a63c79867923cb50b58c4cfde73d9ebb4c901413; its merge tree equals the candidate tree. Independent review found no actionable findings. Exact-head push Verification 37285052782 and pull_request Verification 37285059099, attempt 1, passed 554 Python tests and all required connected runtime steps. Both artifacts contained nine PASS reports and matched their GitHub SHA-256 digests. The bounded live Greenhouse read returned 18 jobs, zero external application requests, and a browser-safe missing-context block without creating an application/document. PR #510's closing receipt contains all SHAs, runs, artifacts, and results.

PR #509 merged as 959f9a36cd32a4c326459a0ba472a24a4abd7d3f; exact-head push 37275627438 and pull_request 37275632400 passed and its independent review/closing receipt is recorded. PR #508 manual capture remains merged as 77cf0825e51f34874f49cc7753e721b60a944ef0. PRs #504 and #506 remain merged and were not redone. The separate Codespaces checkout is preserved.

The root checkout is on codex/core-workflow-trial-answers-20261005 from verified main. The user confirmed GPT-6 Luna High and released the old client. Continue through exact review and CI; after merge, proceed to F manual tracking. Keep broader MJ-REQ-095 and MJ-REQ-100, the combined A-F journey, and full-spec release acceptance PARTIAL until their complete evidence exists. Product AI is not implemented. No private applicant data or retained Windows runtime was used.

At 13:00 America/Chicago on 2026-10-05, stop new features and stabilize through 15:00. Any unmet criterion remains PARTIAL.

---

# ChatGPT iteration handoff — 2026-10-04

## Current authorized milestone — 2026-10-05

The user superseded the prior stop-after-one-iteration instruction and
authorized the time-bounded **CORE-WORKFLOW-TRIAL** from current GitHub `main`.
Current task branch: `codex/core-workflow-trial-20261005`, based on
`44b98897fb1ced728ff206af2576b47c7cb240b1`. The root checkout was clean and
exclusive ownership was confirmed before branch creation; the separate
Codespaces checkout remains preserved. Model selection was user-confirmed as
GPT-6 Luna High; this interface cannot independently inspect billing metadata.
No provider API, private profile, retained Windows runtime or personal database
has been used.

PRs #504 (fact-review reset) and #506 (developer tooling) were verified merged
and were not redone. Existing profile/import/review and document-history
journeys passed focused browser regression checks. The first missing behavior,
issue #507, adds manual job capture to canonical job/posting/route/audit storage.
Its local synthetic browser/API/web acceptance is recorded in
`docs/evidence/2026-10-05-manual-job-capture.json`; it proves no pasted URL is
fetched, differentiates a reference from an application URL, verifies durable
readback/preparation handoff, and keeps external submission OFF. This is a
narrow `PARTIAL` slice, not completion of MJ-REQ-186/188/192 or of workflow B.
Independent review and exact-final-head push/PR CI with all runtime artifacts
are still required before merge. Queue entry MJ-PR-151 records the scope.

Continue the authorized sequence only after the exact-head gates pass. Next,
verify the existing supported public-source UI and perform a bounded actual
public Greenhouse GET separately from controlled fixtures, without starting or
updating the retained private runtime. Then prioritize missing user actions in
matching, preparation, reusable answers and manual tracking. At 13:00 America/
Chicago on 2026-10-05, stop new feature families and stabilize through 15:00.
Any unmet acceptance remains `PARTIAL`; the full 366-requirement release is not
the target. External submission remains OFF throughout.

The older handoff below is historical context; GitHub current `main` and the active user instruction supersede its prior stop and stale checkout observations.

## Current authorized milestone — 2026-10-05

The user authorized sequential implementation of **CORE-WORKFLOW-TRIAL** from current GitHub main, with new features stopping at 13:00 America/Chicago and stabilization through 15:00. Luna High was user-confirmed; this interface cannot inspect billing metadata. The root checkout is task-owned after the user confirmed the older client released. The separate Codespaces worktree remains preserved. No retained private runtime, private resume/profile, paid product AI, or external application submission has been used; external submission stays OFF.

PRs #504 and #506 remain merged and were not redone. PR #508 delivered the narrow manual capture fallback and merged as `77cf0825e51f34874f49cc7753e721b60a944ef0` from candidate `a05294cc1792b81924f01270852b1354b5d67505`. Its exact-head push Verification was run 37268232466; pull_request Verification was run 37268236341. Both passed required runtime reports. The PR #508 closing receipt also records a bounded public Greenhouse live read. Issue #507 was closed only for the narrow manual-capture gap; public-source UI proof and broader jobs requirements remain open.

Current branch `codex/core-workflow-trial-matching-20261005` starts from merged main `77cf0825e51f34874f49cc7753e721b60a944ef0`. PR #509 contains deterministic skill explanations from approved facts, profile-serialized rescoring, manual capture readback, and controlled mock classification. Reviews of candidates 5792dc5, 5937030, 6850cac, and 4328c95 found/repaired the prior issues; the independent review on 4328c95 itself reported no product defects. However, both 4328c95 hosted events had a hidden continue-on-error failure in `cli.ui_source_readback`: its exact text oracle still expected copy replaced by the matching UI. The separate public Greenhouse probe passed, but the browser readback report was missing, so neither event qualifies the head for merge. Source commit `db5f83ed077ed8478a7bfe0ec6e36b82c973718f` updates the runtime oracle to current copy. Local 281 integration tests, focused matching/flywheel tests, Chromium manual-capture, web tests/build, and repository validators passed on the prior matching source; the final candidate must repeat hosted checks. Evidence is `docs/evidence/2026-10-05-explainable-matching.json`; matching remains PARTIAL. Keep PR #509 draft until a fresh exact review and both exact-head events pass with all runtime reports, especially the conditional public-source UI readback. The optional AI dependency remains preserved; MJ-PR-152 is the deterministic slice. Record final candidate/base/run/artifact/merge IDs in the PR closing receipt, avoiding a self-referential commit.

After matching, finish the existing supported-source Jobs UI and confirm its bounded live-read behavior, then prioritize job-specific documents, reusable answers, and manual application history by the next user action they unblock. At 13:00 stop starting feature families and stabilize. Any missing acceptance remains PARTIAL; this milestone does not claim all 366 requirements or full release readiness.


The user requested that local delivery finish, publish its work, stop the local
runtime, and hand off for ChatGPT Web Pro iteration. No next feature was started.
Use latest GitHub `main`; old branch/session records are evidence, not ownership.
Start with [the copyable handoff prompt](../prompts/CHATGPT_WEB_HANDOFF.md).

## Verified implementation checkpoint

- Last implementation PR: [#501](https://github.com/MulattoTech/MJobs/pull/501).
- Tested candidate: `737d77732934fc903a416d71764eecc684ff5a25`.
- Implementation merge: `a0bf00690d6f9aa834e4e0001a3ae19efb35f055`.
- Exact-head Verification: push **37239204735 / job111544310647** and PR
  **37239207108 / job111544317989**, attempt 1. Both workflows and all 16 runtime
  artifacts passed, including connected contact-header UI and app-image tests.
- Local evidence: 534 Python regressions, 45 web tests/build, final focused fidelity
  checks, actual browser acceptance, 18 Linux app-image tests, independent review.
- [Exact receipt](../docs/evidence/2026-10-04-contact-header-exact-head-ci.json),
  [implementation evidence](../docs/evidence/2026-10-04-resume-contact-header.json),
  [all 15 session merges](../docs/evidence/2026-10-04-delivery-merge-index.json).

Final handoff publication [PR #502](https://github.com/MulattoTech/MJobs/pull/502)
contains documentation/planning/evidence plus a scoped personal-mode browser
locator repair discovered during final CI. Its own
final-head CI and merge receipt are recorded in that PR's closing comment. The
implementation checkpoint above is not a fixed future main SHA.

## Accepted and remaining work

**MJ-PR-024 and MJ-PR-025 are PASS**, backed by lifecycle/source-authority,
description-versioning, career/contact/preferences, native import, date review,
and bounded offline scanned-PDF OCR evidence. A merged prerequisite alone was
not treated as proof of remaining acceptance.

**MJ-REQ-092–094 and 096–099 are accepted**: immutable approved-fact history and
provenance, seven document collections, assertion gates, ATS layouts, consistent
DOCX/PDF/TXT, and generated/uploaded original comparisons with extraction/OCR
uncertainty. See the capability matrix and linked receipts.

**MJ-REQ-095 / issue 96 remains PARTIAL and OPEN.** Verified: skill ordering,
explicit approved-fact selection/omission, matched skill/technology emphasis,
and optional saved-contact headers. Broader summary/bullet rephrasing and
automatic keyword rewriting remain incomplete. MJ-PR-027's own requirements are
accepted, but its queue status remains PARTIAL because MJ-PR-026 is incomplete.

## Fact-editor regression #503 — accepted in PR504

PR504 merged as `b1691622a61ee368f058461ff002728325d275a9` from exact candidate `5f535d90f8f3b91e1a16bf1c5eb9a667b430d753`.
Text/source/kind edits reset both review flags. Three real browser journeys
cover persisted exclusion, verification-only exclusion, explicit reapproval,
stable identity, current TXT/DOCX/PDF and all historical document hashes.
Push and PR Verification each passed537 Python tests and all9 runtime reports;
independent static review found no actionable defects after its CI finding was
fixed. [Exact receipt](../docs/evidence/2026-10-04-fact-review-exact-head-ci.json).

The explicitly requested developer-tooling continuation is issue505: bounded
Codespaces/Codex CLI setup. Its offline bootstrap contract and shell checks
passed; actual devcontainer smoke, final-head Verification and independent
review remain required. The final exact-head and merge receipt belongs to the
[PR506 closing comment](https://github.com/MulattoTech/MJobs/pull/506), which supersedes this pre-merge snapshot. No automatic next iteration is authorized.
[Scoped evidence](../docs/evidence/2026-10-04-codespaces-bounded-iteration.json);
[new entrypoint](../prompts/CODESPACES_ITERATION.md).

The original Windows checkout is still clean at c89a09c and was not mutated or
deployed; an old client remained present, so this iteration used a new owned
clone at C:/Tools/Dev/MJobs-fact-review-503. Private files, retained volumes and
the separate prior Codespaces checkout were preserved. No billable Codespace
was created. No new product feature or autonomous continuation is authorized.

AI provider/task/cost-control work remains unimplemented. Two isolated small
local-model probes produced schema-valid but irrelevant selections and failed
the same oracle; [failure evidence](../docs/evidence/2026-10-04-local-ai-feasibility.json).
Do not relabel those probes as production AI acceptance.

## Local shutdown and preservation

- Main checkout: `C:/Tools/Dev/MJobs`. Closeout returns it to clean synchronized
  main and releases its private owner receipt.
- Eight mjobs project containers stopped gracefully. Database, documents, queue
  and mock-receipt volumes retained. Other applications and Docker daemon kept
  running. No production deployment occurred.
- Local feedback GUI stopped after verifying its module and checkout path; saved
  feedback/drafts preserved. Ports 18700–18703 have no listeners.
- Fifteen merged implementation branches retired locally/remotely after exact
  PR-head/ancestry checks; all commits remain in main.
- Seven unused test-image tags and isolated 1.88 GB model cache removed. No global
  Docker prune or unrelated model deletion.
- Ignored .env, private resumes/feedback/documents, outputs, dependency environments
  and diagnostic receipts retained locally and uncommitted.
- Independent clean `C:/Tools/Dev/MJobs-codespaces` preserved on
  `codex/codespaces-devcontainer-20261004`, observed at
  `9b0d508ad554b6ca847a73dec5b6ce797014fd10`. Reconcile its owner before changes.
- [Cleanup receipt](../docs/evidence/2026-10-04-session-closeout.json).

When local testing resumes, `./Start-MJobs.ps1` rebuilds and starts the existing
fictional mjobs workspace at 18700 with retained data. This is not PERSONAL mode.
Personal startup uses `-Personal` and a separate project; consult the workspace
mode runbook before switching. External submission stays OFF. Remote read-only
review does not require starting the local runtime.

Keep AGENTS.md, current issues/specs, IMPLEMENTATION_STATE, PR_QUEUE,
CAPABILITY_MATRIX and exact-head receipts authoritative. Older feature dashboards
are dated snapshots. The original handoff ended without an active implementation
lane; the authorized local-only continuation and its current target are recorded
below.

## 2026-10-06 local-only continuation — MJ-REF-013

Issue #383 / MJ-REF-013 is implemented, locally PASS, and CLOSED at 2026-10-06T05:26:16Z with all five acceptance boxes checked and read back. Candidate `60f2addd1b73c06dcc0f206369a4ee0b78c155e1` was based on `6d92e2d22024cce51a7189d5ed3b9ef6c878c142` and integrated to synchronized main by a non-force fast-forward. The exact tree is `cb3ea397e4aedc39fd41ba293f55e4516d53a45f`. The evidence receipt is [2026-10-06-bounded-agent-toolregistry.json](../docs/evidence/2026-10-06-bounded-agent-toolregistry.json); final planning receipt commit is `8b9370c88b20654e7c9a45795525a3fa70194823`. The full local suite passed 642 tests; Actions and hosted reviews remain NOT_RUN under the budget policy. External submission remains OFF.

Next selected dependency-ready slice: issue #384 / MJ-REF-014. Its remaining acceptance criterion is that task prompts receive no unrelated sensitive profile sections by default. Verify all local prompt-construction paths and keep the requirement denominator unchanged; no paid/hosted provider call is authorized.


## 2026-10-06 local-only continuation — MJ-REF-014

Issue #384 / MJ-REF-014 is locally PASS and source commit `5e9d630564a2327838af6d73bebbfd08f3d16a83` is integrated to synchronized `main` by non-force fast-forward. The provider-neutral task prompt builder supplies no profile context by default; explicitly requested facts flow through approval, task scope, denylist, cross-profile checks, and value-free audit. Focused tests (39), full local Python/browser suite (648), frontend build, and repository validators passed. See [task prompt privacy receipt](../docs/evidence/2026-10-06-least-data-task-prompts.json). GitHub issue checkbox/closure readback and final planning receipt remain to be recorded. Hosted Actions/reviews and AI provider calls remain NOT_RUN by policy; external submission stays OFF. The original requirement denominator is unchanged.


Issue #384 closeout: GitHub readback at `2026-10-06T05:57:44Z` confirms CLOSED with all five acceptance boxes checked; closure comment is https://github.com/MulattoTech/MJobs/issues/384#issuecomment-6010322555. Exact evidence includes source integration, 648-test local suite, focused tests, build and validator results.

Next selected target is open P0 issue #5 / MJ-REQ-004 Bound External Operations. The matrix still says NOT_RUN and merged narrow worker lifecycle work is not broad acceptance. Inventory and address timeouts, captured output, controlled cancellation, retries and error classification across hanging HTTP, browser, AI, parsing/OCR, process, source and ATS paths; preserve the denominator and keep acceptance PARTIAL until fully evidenced. Local-only policy and external-submission OFF remain active.


## 2026-10-06 local-only continuation — MJ-REQ-004 first P0 slice

Open issue #5 / MJ-REQ-004 remains PARTIAL. Candidate `2b84f9cf610a65791a135a0f5bfc70133b3235af` is integrated by non-force fast-forward to synchronized main. Durable worker tasks now enforce a monotonic deadline at cooperative checkpoints and persist `FAILED / OPERATION_TIMEOUT` rather than late ordinary success; no automatic timeout retry is added. Greenhouse production rate waits poll cancellation every 250 ms before the next GET. Cancellation and timeout leave prior source health/failure/backoff history intact. See [operation-deadline evidence](../docs/evidence/2026-10-06-bound-external-operation-deadlines.json). Local evidence: 72 focused integration tests, 652 full Python/integration/Chromium tests, 62 frontend tests, build, catalogue/progress/skill validators all PASS.

This does not close #5: AI provider requests are NOT_RUN because no provider implementation exists; full browser/parser/OCR/process/source/ATS timeout, output, retry and classification evidence remains. A local npm audit found one high advisory only in the development tool dependency tree (`source-map-js` via Vite); production dependency audit reported zero. Node v25.9.0 is outside Vitest's declared engine range, though build and tests passed. No dependency files were changed. Continue the P0 audit with bounded CPU-heavy resume parsing and browser action deadlines. External application submission stays OFF; hosted Actions/reviews remain NOT_RUN.


Planning receipt synchronization: `a87704e7c256f14d0fe7c323e59a908b357dad07` is verified on local and remote main; the exact issue #5 slice remains PARTIAL. The immediate follow-on audit is hard wall-clock bounds for local PDF/DOCX extraction, because current byte/page/resource bounds do not prove a hard extraction deadline.


## 2026-10-06 local-only continuation — MJ-REQ-004 native parser slice

Issue #5 / MJ-REQ-004 remains OPEN/PARTIAL. Native PDF and DOCX parsing now runs in a shell-free child with a 15-second wall deadline, bounded JSON output, a credential-free environment allowlist, and Linux address-space, CPU, file-size, and core-dump limits. OCR stays parent-side under its existing local bounds. Candidate `492eb8f9d83d4e2e0f1e8af9e6b18b0ab31b922a` was non-force fast-forwarded to synchronized local/remote main. Evidence: [bounded native resume parsing](../docs/evidence/2026-10-06-bounded-native-resume-parsing.json). The 47-test affected profile/parser/OCR/comparison/browser suite, 32 Linux-container parser/OCR tests, 654-test full local Python/integration/browser suite, and catalogue/progress/skill validators passed. The original 366-requirement denominator is unchanged.

The parser slice preserves review-before-save and profile isolation. Timeout, child launch, nonzero exit, malformed output, existing DOCX XML bounds, native extraction, OCR fallback and resume history were exercised. Actions/hosted reviews and AI-provider calls remain NOT_RUN; request-disconnect cancellation and production deployment were not tested. External application submission remains OFF.

Next continue issue #5 by auditing supported ATS/Playwright navigation and selector waits for operation deadlines, cooperative cancellation, captured output, and safe error classification. Preserve the existing `SUBMISSION_UNCONFIRMED` no-blind-retry behavior. Do not close #5 or mark MJ-REQ-004 PASS until every applicable operation category is evidenced.


## 2026-10-06 local-only continuation — MJ-REQ-004 mock ATS deadline boundary

Issue #5 / MJ-REQ-004 remains OPEN/PARTIAL. The local Playwright mock ATS flow now rechecks cancellation/deadline after navigation and upload preparation, and rechecks the worker deadline after the durable SUBMITTING reservation immediately before the irreversible Submit click. When the deadline expires in that gap, no click occurs; absent receipt is reconciled as SUBMISSION_UNCONFIRMED and must not be blindly retried. Candidate `e86e8d0ff2ada3b357961c925a33584ae50d0412` was integrated to local/remote main by non-force fast-forward. Evidence: [mock ATS submit deadline](../docs/evidence/2026-10-06-mock-ats-submit-deadline.json). The focused browser/worker/API tests (31), full local suite (656), and repository validators passed.

One unrelated Uvicorn test-server teardown timed out in the first full-suite run. Systematic investigation found the same test passed alone and with its module, then the full suite passed on rerun without changing that test. The failed attempt and follow-up are recorded in the evidence. External submission remains OFF; real ATS, hosted verification/reviews and AI provider calls remain NOT_RUN.

Next continue issue #5 by auditing remaining public-company/source HTTP and external process paths for timeouts, cancellation, captured-output limits, retry policy, and safe classification. Preserve the current 366-requirement denominator; do not close #5 or mark MJ-REQ-004 PASS from these scoped slices.


## 2026-10-06 local-only continuation — MJ-REQ-004 verifier Git deadline

Issue #5 / MJ-REQ-004 remains OPEN/PARTIAL. `tools/verification/fact_review.py` now resolves Git HEAD with a five-second shell-free subprocess, discards stderr, validates the SHA, and writes a safe FAIL evidence receipt if Git times out or fails. Candidate `839c9f0b164a2bef9d93dd1ec64f580a59f7c98f` was non-force fast-forwarded to synchronized main. Evidence: [bounded fact-review Git lookup](../docs/evidence/2026-10-06-bounded-fact-review-git.json). Seven focused unit tests, the four-case Chromium verifier on clean main, the 659-test full suite, and catalogue/progress/skill validators passed.

A pre-commit verifier smoke run occurred on a dirty checkout and captured the prior HEAD; it was excluded from candidate evidence. The verifier was rerun after integration on clean main and reported the exact candidate SHA. Next add a dirty-worktree/source-tree identity guard to prevent stale SHA receipts, then continue the remaining subprocess and public-company/source HTTP audit. External submission stays OFF; Actions/hosted reviews remain NOT_RUN; issue #5 must remain open until broad acceptance is fully evidenced.


## 2026-10-06 local-only continuation — MJ-REQ-004 clean-tree evidence guard

Issue #5 / MJ-REQ-004 remains OPEN/PARTIAL. The fact-review verifier now refuses to certify a source SHA when tracked or untracked worktree changes exist. Its Git revision and status calls are shell-free, each bounded to five seconds, and store only fixed safe categories without paths/raw diagnostics. Candidate `895b418e84d47a00ca6b7f22f032b401a84460a9` is integrated by non-force fast-forward to synchronized local and remote `main`. Evidence: [fact-review clean-tree guard](../docs/evidence/2026-10-06-fact-review-clean-tree.json).

Nine focused unit tests, the dirty-tree refusal gate, clean four-case Chromium verifier with exact source SHA, catalogue/progress/agent-skill/diff validators passed. The 661-test full suite had one unrelated timing-sensitive application-pipeline server-thread shutdown assertion; isolated rerun passed (660 full-suite tests passed, zero skips). The initial system-Python browser attempt lacked Playwright; the repository `.venv` run passed. Hosted Actions/reviews remain NOT_RUN by policy and external submission stays OFF. Requirement denominator remains 366 requirements and 59 reference gaps.

Next continue issue #5 by auditing aggregate subprocess output/time/resource bounds and public-company/source HTTP timeout, cancellation, retry, payload, and safe error classification. Do not mark broad acceptance PASS or close #5.


## 2026-10-06 local-only continuation — MJ-REQ-004 mock ATS remaining deadline

Issue #5 / MJ-REQ-004 remains OPEN/PARTIAL. Worker cancellation checks now expose the remaining monotonic budget, and local mock ATS browser launch, navigation, each waiting locator action, Submit, and confirmation waits are capped by that budget. The final Submit gate and durable receipt reconciliation remain in place. Source `5680d0e317b8614b214b0e93a4b9d4fda799ffd0` is integrated by non-force fast-forward to synchronized main. Evidence: [mock ATS action budget](../docs/evidence/2026-10-06-mock-ats-action-budget.json).

Exact integrated main passed 12 mock application browser tests, 12 worker lifecycle tests, and the 663-test full local suite (zero failures, errors, or skips). The first exact-head suite used a stale ignored `apps/web/dist` and produced one failure plus one error; rebuilding the current frontend source fixed both, and isolated reruns plus the final exact-head full suite passed. The production frontend build and repository validators passed.

Hosted Actions/reviews and external web-search, AI-provider, and real ATS calls remain NOT_RUN/BLOCKED_EXTERNAL under current configuration and policy. External submission stays OFF; the 366-requirement and 59-reference-gap denominators are unchanged.

Next continue the local issue #5 audit for remaining subprocess/source operation bounds and error/retry classification. Keep issue #5 OPEN/PARTIAL until broad acceptance is evidenced.

## 2026-10-06 local-only continuation — MJ-REQ-005 category admission

Issue #6 / MJ-REQ-005 now has a bounded local implementation candidate. Durable operation kinds map to independent source collection, company discovery, document generation and application automation ceilings; AI has a validated configuration ceiling but no runtime operation. PostgreSQL claims serialize by category across operation kinds; SQLite and isolated PostgreSQL tests verified admission. Evidence: [operation concurrency categories](../docs/evidence/2026-10-06-operation-concurrency-categories.json).

Candidate `23104373877a275c97204f7e1af08c4cd646b4d9`, based on `8123dbff150a7a86ec200ce1cd0a2422bd3ec984`, passed 21 focused SQLite/Compose tests, one isolated PostgreSQL concurrent-claim test, 35 browser tests, and the 669-test Python suite (zero failures/errors, one optional PostgreSQL skip). TypeScript/Vite build and catalogue/progress/skill validators passed. The first full run lacked ignored `apps/web/dist`; after the current frontend build, the browser suite and complete rerun passed.

Keep #6 OPEN/PARTIAL: no AI runtime provider/operation exists, real ATS automation remains OFF, and the legacy persisted per-source Greenhouse concurrency value is metadata rather than an active per-source parallel-fetch control. No requirement denominator changed (366 requirements, 59 reference gaps). Hosted Actions/reviews remain NOT_RUN by policy. External submission remains OFF.

Source 23104373877a275c97204f7e1af08c4cd646b4d9 and its evidence/planning receipt are integrated by non-force fast-forward; local and remote main matched 9f5af9fce6a367f084cbdc144364076f88cd6a5a, with no protection rule or PR. #6 remains OPEN/PARTIAL. Continue issue #5 / MJ-REQ-004 by selecting the next locally implementable external-operation bound or classification gap after reconciling current code. Do not wait for a user continue prompt.


## 2026-10-06 local-only continuation — MJ-REQ-006 current workspace inspection

Issue #7 / MJ-REQ-006 was inspected read-only at `bd00c331d503009189a83481ba62a56a66acb75c`. The current populated MJobs repository has a clean synchronized main, Python/FastAPI plus PostgreSQL/Alembic/Redis/RQ backend, React/TypeScript/Vite frontend, Playwright/browser coverage, Docker Compose, private `.env` and tracked `.env.example`, and disabled GitHub Actions. Existing source, tests, migration history, Compose runtime, worktrees and private outputs were classified KEEP; the private `.env` and output contents were not opened. Evidence: [current workspace inspection](../docs/evidence/2026-10-06-current-workspace-inspection.json).

No application code or runtime services were changed; this issue acceptance is inventory/classification only. After integrating the evidence and closing #7 on exact readback, continue the locally actionable P0 #6 / MJ-REQ-005 source-concurrency truthfulness gap. Denominators remain 366 requirements and 59 reference gaps; hosted Actions/reviews remain NOT_RUN; external submission remains OFF.


Evidence and planning for issue #7 are integrated on synchronized main at 124b084925a807079cce78c168211f73b7bf97fb; GitHub issue #7 is CLOSED (read back at 2026-10-06T09:12:32Z). The inspection requirement is PASS; overall capability remains PARTIAL. The next selected dependency-ready issue is #6 / MJ-REQ-005: inspect and, if safely supported by current source architecture, enforce its persisted per-source concurrency cap while keeping AI/real ATS portions PARTIAL. External submission remains OFF; Actions and hosted review remain NOT_RUN.


## 2026-10-06 local-only continuation — MJ-REQ-005 Greenhouse request concurrency

The persisted Greenhouse source concurrency setting now limits concurrent board-metadata and job-inventory reads. `1` keeps the reads sequential; `2` permits both independent GETs to overlap; larger settings remain bounded by the two requests available in a search. Parallel requests reserve their starts on one shared rate schedule. The adapter preserves provider-failure status when a sibling read succeeds, and a local cancellation/deadline remains authoritative over a concurrent network error. The Sources UI explains the control; real Chromium saved `2` and read it back after reload. Implementation: `ec28943ec20109c2b1bc12c0130a26a2991b0636`; evidence: [per-source Greenhouse concurrency](../docs/evidence/2026-10-06-per-source-greenhouse-concurrency.json).

Verification: 29 focused Greenhouse/source-scheduler tests PASS; core Chromium workflow PASS; all 35 browser tests PASS; TypeScript/Vite production build and 62 frontend tests PASS; repository validators PASS. Exact-head full discovery passed 674 tests with one optional PostgreSQL concurrency test skipped because `MJOBS_TEST_POSTGRES_URL` was unset. Its first run had one Uvicorn thread-shutdown timeout; that test passed alone and in the browser suite, and the same-head full rerun passed. The failure is retained in evidence. No live employer request or application submission occurred; Actions and hosted review remain disabled/NOT_RUN.

Issue #6 / MJ-REQ-005 stays OPEN/PARTIAL: source request concurrency and the existing category admission controls are locally evidenced, but there is no AI runtime/provider operation and real ATS automation remains unavailable/OFF. Requirements remain 366 and reference gaps 59. The source candidate is pushed on `codex/req005-source-concurrency-20261006`, based on synchronized main `795bc74d10cae145e40c628daa42a285060e3280`; no PR is open. After integrating evidence/planning by safe non-force fast-forward, continue with live reconciliation of P0 issue #5 / MJ-REQ-004 and select the next locally implementable operation-safety slice.


Integration receipt (2026-10-06T10:13:08Z): issue #6 source `ec28943ec20109c2b1bc12c0130a26a2991b0636` plus evidence/planning are integrated on synchronized local and GitHub `main` at `2a55de01a6aed215363b6bdfcb6c2029d1658553` by non-force fast-forward. `main` had no protection rule; Actions were disabled; no PR was opened. GitHub issue #6 comment/readback confirms OPEN. The requirement remains PARTIAL. Next: reconcile issue #5 / MJ-REQ-004 on this main.


## 2026-10-06 local-only continuation — MJ-REQ-004 bounded OCR diagnostics

Candidate `3d604b27b1a4c25e2ff0ee6fa68a07e9ff8f74a0` on `codex/req004-bound-ops-20261006`, based on current main `5b706edb52945dc16a71f86c95b14a8b3bda023a`. OCR now captures child stderr in a temporary file under the Linux child file-size limit, retains only capped diagnostic-size metadata, and reports sanitized timeout, launch, nonzero-exit, and missing-output warnings. Raw diagnostics are never returned, logged, or persisted. Native text survives OCR failures; facts remain review-only. Chromium shows the warning and extraction does not mutate profile data.

Validation: 48 focused unit/parser/API/Chromium tests PASS; all 35 browser tests PASS; all 680 full Python/integration/browser tests PASS on the final exact-head run with one optional PostgreSQL test skipped because `MJOBS_TEST_POSTGRES_URL` was unset; all 62 frontend tests and build PASS; isolated no-network Linux app-image OCR/browser acceptance and synthetic exit-7 bounded-diagnostic checks PASS; repository validators PASS. A stale ignored `apps/web/dist` caused one initial browser assertion and was rebuilt. A subsequent full run had one unrelated Uvicorn shutdown timeout; the case passed alone and in the browser suite, and the same-head full rerun passed. No timeout was changed.

Evidence: [bounded OCR diagnostics](../docs/evidence/2026-10-06-bounded-ocr-diagnostics.json). Issue #5 / MJ-REQ-004 remains OPEN/PARTIAL: request-disconnect cancellation, real AI/ATS operations, hosted checks and remaining operation-category policy are outstanding; external submission remains OFF. Denominators remain 366 requirements and 59 reference gaps. The source commit is pushed; evidence/planning integration receipt is pending.


Integration receipt (2026-10-06T10:57:12Z): issue #5 source candidate `3d604b27b1a4c25e2ff0ee6fa68a07e9ff8f74a0` and evidence/planning are integrated on synchronized local and GitHub `main` at `fe598c1ee351010502a62e8c9dc6b76c83692ffe` by non-force fast-forward. `main` had no protection rule, Actions were disabled, and no PR was opened. GitHub issue #5 comment/readback confirms OPEN. MJ-REQ-004 remains PARTIAL. Continue with request-disconnect cancellation and remaining locally implementable operation boundaries.


## 2026-10-06 local-only continuation — MJ-REQ-004 public HTTPS cancellation

A pending public-company HTTPS connect/read now polls the existing durable worker cancellation/deadline callback. When it raises, the client interrupts and closes only that request's sockets, then returns the original exception instead of misclassifying cancellation as a network error. The new synthetic blocked-response test failed before implementation. Source candidate `8890af69c66870dd1d43f7fadb78d0bbbd86adc7`, based on `414f6801291bfca602c7f2a0aee40ad246e8297a`; evidence: [docs/evidence/2026-10-06-public-http-cancellation.json](../docs/evidence/2026-10-06-public-http-cancellation.json).

Verification on the exact candidate: public-client tests 23 PASS; focused public discovery/authorization/monitoring unit and integration tests 54 PASS; connected Chromium discovery workflow 1 PASS; full local Python/unit/integration/browser discovery 704 PASS, zero failures/errors, one optional PostgreSQL skip because MJOBS_TEST_POSTGRES_URL was unset. The known feedback-GUI client disconnect traceback appeared during cleanup. No live employer fetch occurred; AI/provider and real ATS operation classes remain unavailable/OFF, hosted Actions/reviews remain NOT_RUN by policy, and external submission remains OFF. MJ-REQ-004 stays PARTIAL and GitHub issue #5 OPEN. Source is committed locally; evidence/planning validators agent_skills, catalogue and progress all PASS. Evidence/planning push and safe integration are next.


Integration receipt (2026-10-06T13:54:22Z): issue #5 source 8890af69c66870dd1d43f7fadb78d0bbbd86adc7 and evidence/planning were integrated by non-force fast-forward on synchronized local and GitHub `main` at 370da80fd291d2597f4492109a9e6d0d15e90211. Actions remain disabled, `main` is unprotected, and no PR was opened. GitHub issue #5 remains OPEN/PARTIAL after evidence comment/readback: https://github.com/MulattoTech/MJobs/issues/5#issuecomment-6017780550. Continue the local issue #5 audit without waiting for providers or hosted checks.


## 2026-10-06 local-only continuation — MJ-REQ-004 Greenhouse response cancellation

Configured Greenhouse GETs now poll the durable operation cancellation/deadline callback while waiting for response headers or body. Cancellation closes the response/client, preserves the original error, and does not mark the provider unhealthy or commit partial jobs. A synthetic stalled-body regression failed before the implementation. Candidate 7b586455880b1bde22fecd79b89810be8a27093a, based on 6688037a2275138b646df2b050f145e70f9e3fe4; evidence: [docs/evidence/2026-10-06-greenhouse-http-cancellation.json](../docs/evidence/2026-10-06-greenhouse-http-cancellation.json).

Verification: 29 Greenhouse integration tests PASS; connected Chromium collection workflow PASS; full browser suite 36 PASS; exact-head full suite rerun 706 PASS, zero failures/errors, one optional PostgreSQL skip (MJOBS_TEST_POSTGRES_URL unset). The first full run had one unrelated application-pipeline server-thread teardown failure; the isolated case and all browser tests passed, followed by a passing same-head rerun. One initial test selector typo was corrected before the correct browser test passed. No live employer request occurred, and external submission remains OFF. Issue #5 / MJ-REQ-004 remains OPEN/PARTIAL; Actions/reviews remain NOT_RUN by policy. Source committed locally; evidence/planning validators and safe integration are next.


Integration receipt (2026-10-06T14:19:05Z): issue #5 source 7b586455880b1bde22fecd79b89810be8a27093a and evidence/planning are integrated by non-force fast-forward on synchronized local and GitHub `main` at 973bc50921eae4e242752b2527321069573e8c86. Actions remain disabled, `main` is unprotected, and no PR was opened. GitHub issue #5 remains OPEN/PARTIAL after comment/readback: https://github.com/MulattoTech/MJobs/issues/5#issuecomment-6018276394. Continue the local P0 issue audit without waiting for providers or hosted checks.


## 2026-10-06 local-only continuation — MJ-REQ-004 local fixture discovery bounds

The explicit local MockClient now streams fixture responses under its 2 MB size limit and polls durable worker cancellation/deadline checks while the request is active. Cancellation closes the response/client and preserves the original error. A synthetic oversized-body and stalled-response regression failed before implementation after correcting the fixture transport setup. Candidate 9b95debda403cbf38b4b147be549ece4bc515b4e, based on 57bb3e6cf4e79fb86edab0d4574a87a5323c151b; evidence: [docs/evidence/2026-10-06-mock-discovery-bounds.json](../docs/evidence/2026-10-06-mock-discovery-bounds.json).

Verification: 31 focused unit/integration/browser tests PASS; full local suite 708 PASS, zero failures/errors, one optional PostgreSQL skip because MJOBS_TEST_POSTGRES_URL was unset. No live employer request or external submission occurred. Issue #5 / MJ-REQ-004 remains OPEN/PARTIAL; hosted Actions/reviews remain NOT_RUN by policy. Source committed locally; evidence/planning validators and integration are next.


Integration receipt (2026-10-06T14:32:22Z): issue #5 source 9b95debda403cbf38b4b147be549ece4bc515b4e and evidence/planning are integrated by non-force fast-forward on synchronized local and GitHub `main` at 96e2c92fcae184e79b7d397f4cc1ced275afa051. Actions remain disabled, `main` is unprotected, and no PR was opened. GitHub issue #5 remains OPEN/PARTIAL after comment/readback: https://github.com/MulattoTech/MJobs/issues/5#issuecomment-6018530633. Next reconcile dependency-ready P0 issue #8 / MJ-REQ-007.


## 2026-10-06 local-only continuation — MJ-REQ-108.A06 disabled provider

The existing dependency-ready `MJ-PR-030` queue item is active for child issue #345 only. Commit `678aee64243e587608c6580cfaf59064fe42d56e`, based on main `e2b199944a9b3c5080480ea3194b82bd6884053f`, adds a provider-neutral contract and a disabled/no-AI implementation that declares zero capabilities, refuses generation without echoing prompts/tasks, and makes no network call. Evidence: [docs/evidence/2026-10-06-disabled-ai-provider.json](../docs/evidence/2026-10-06-disabled-ai-provider.json).

Verification on the committed source: focused contract tests 2 PASS; complete unit suite 318 PASS; agent-skill, catalogue, progress journal and diff validators PASS. There is no browser workflow or safe external call for a provider that is intentionally disabled. Hosted checks/reviews remain NOT_RUN. The broader parent requirement MJ-REQ-108 remains PARTIAL until real provider implementations and consumer wiring are completed. The branch was integrated by non-force fast-forward to synchronized local and GitHub main at 9e340924878484ba257097a5088d83ab10000b3a; issue #345 was closed after scoped evidence comment/readback. Parent issue #109 remains OPEN/PARTIAL.


Integration receipt (2026-10-06T18:54:23Z): source/test commit 678aee64243e587608c6580cfaf59064fe42d56e plus evidence/planning are integrated at synchronized local and GitHub main 9e340924878484ba257097a5088d83ab10000b3a. GitHub issue #345 is CLOSED after evidence readback (https://github.com/MulattoTech/MJobs/issues/345#issuecomment-6023248419); parent #109 remains OPEN/PARTIAL. The separate Antigravity Remotive worktree remains dirty on its own branch and was not modified.


## 2026-10-06 next target — MJ-REQ-109 task configuration

Next implementation target is GitHub issue #110 / `MJ-REQ-109` under the existing dependency-ready `MJ-PR-030`. Implement independent task-specific provider/model selection with disabled defaults and fail-closed unsupported providers; no external or paid AI call is needed. The separate Antigravity worktree is implementing Remotive under `services/discovery/sources`; keep this slice in `services/ai` and its tests/planning only.


## 2026-10-06 local-only continuation — MJ-REQ-109 task configuration

Issue #110 / `MJ-REQ-109` now has an in-process seven-task provider/model router in candidate `49f4769df3b06e413c25b3ab9a2282db74369e5b`, based on main `c7656bbecec61e2d037bc57869fad4e1ee8f43d4`. Omitted tasks stay on `disabled`; unknown tasks, invalid/secret-shaped model identifiers, and unimplemented provider calls fail closed. Evidence: [docs/evidence/2026-10-06-ai-task-configuration.json](../docs/evidence/2026-10-06-ai-task-configuration.json). Focused AI tests 8 PASS; complete unit suite 324 PASS; compile, catalogue, progress, agent-skill and diff validation PASS. No UI, persistence, production consumer, or remote provider integration is claimed. Issue #110 remains OPEN/PARTIAL. Candidate branch push and safe integration are pending.
