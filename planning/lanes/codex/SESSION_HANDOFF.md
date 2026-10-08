# Codex lane handoff

Status: **IDLE_READY / #177 MJ-REQ-176 partial slice integrated at bd251813996deac05010d1d080a7ec5671a0dbd9**

Issue #177 remains OPEN/PARTIAL. `APPLICATION_SUBMITTED` is now appended only after the local MOCK ATS submit click returns or a valid fixture receipt is recovered; confirmation remains separate and unconfirmed outcomes are preserved. Evidence: `docs/evidence/2026-10-07-application-submit-audit.json`. Affected 34-test suite, frontend 63/build and local validators PASS. Full Python suite ran 962, 8 skipped, one unrelated company-discovery Uvicorn shutdown assertion failure; isolated rerun passed. Claim release comment: https://github.com/MulattoTech/MJobs/issues/177#issuecomment-6045401912.

Next: reconcile current main and all active claims/worktrees before selecting the next unclaimed dependency-ready issue. Hosted checks NOT_RUN_BY_USER_POLICY; external submission OFF.

---

# Codex lane handoff

Status: **IDLE_READY / issue #179 / MJ-REQ-178 Log Retention PASS**

## Completed issue #179 / MJ-REQ-178

Branch: `codex/mj-req178-log-rotation-20261007`
Worktree: `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req178-log-rotation-20261007`
Base main: `9a61c6e4ae3b667cd3556b867562ff72900f8a9c`
Claim: https://github.com/MulattoTech/MJobs/issues/179#issuecomment-6040940208
Expected files: `compose.yml`, `tests/unit/test_compose_modes.py`, `docs/evidence/2026-10-07-compose-log-rotation.json`, lane-local state.

Acceptance gap reproduced: Docker Compose rendered all nine services without a `logging` bound. A focused regression test failed with missing `logging` for all nine. Source `1e11348ec1e10f401453e1d31ba68d651ea2c540` adds a shared `json-file` rotation policy at 10 MiB × 5 files for all nine services. Focused Compose/live-feedback/document-artifact tests: 20 PASS; validators pass using `.venv`. Evidence `docs/evidence/2026-10-07-compose-log-rotation.json` is being recorded. A disposable no-network Docker container verified actual rollover (50 large lines emitted, 11 retained with a 1 MiB × 2 cap, then removed); the running personal MJobs services were untouched. Existing feedback code removes expired managed sessions and disables raw DOM traces; preserve this behavior and reconcile existing evidence before deciding whether issue #179 is fully accepted or remains partial. External submission OFF; hosted checks NOT_RUN_BY_USER_POLICY.

Integrated PASS at main 8bea57e595e19e638913405a8ec1cd3aa0ebde32; GitHub issue closed/release comment https://github.com/MulattoTech/MJobs/issues/179#issuecomment-6041087412. Local claim released. Next: release integration lock, reconcile all active claims and dirty worktrees, then select and claim the next highest-value dependency-ready issue.

---

Status: **ACTIVE / issue #6 / MJ-REQ-005**

## Completed issue #390 / MJ-REF-020

Integrated on main `300cf704915574d4e33beb251af14b003bd384d9`; issue closed with sanitized receipt at https://github.com/MulattoTech/MJobs/issues/390#issuecomment-6036388938. Evidence: `docs/evidence/2026-10-07-external-application-capture.json`.

## Active issue #6 / MJ-REQ-005

Branch: `codex/mj-req005-postgres-concurrency-20261007`
Worktree: `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req005-postgres-concurrency-20261007`
Base: `300cf704915574d4e33beb251af14b003bd384d9`
Candidate: `9e282135e9c90d56301b9d8726479479d84a862a` (tree `fd7665952a141c07a494f3a20b9986766363085b`)
Evidence target: `docs/evidence/2026-10-07-postgres-concurrency-runtime.json`

The prior skipped PostgreSQL category-admission proof now ran against an ephemeral, fresh local PostgreSQL 17 container. Added a cross-kind `APPLY_MOCK` capacity-one race across independent Store engines, with durable `RUNNING`/`QUEUED` state and attempts readback. Both company-discovery and application-automation PostgreSQL race tests plus operation-category unit and worker lifecycle tests passed: 21 tests, repeated twice on the same isolated DB. The first re-run against a reused test database correctly exposed old RUNNING test rows; regression cleanup now marks only its unique test-owned rows CANCELLED in `addCleanup`, and repeated execution passes. The existing personal MJobs PostgreSQL container was never used or restarted.

This is a verified slice, not full MJ-REQ-005 completion: no real ATS provider/browser submission was exercised; external submission remains OFF. Hosted checks remain NOT_RUN_BY_USER_POLICY.

Other active lanes: Antigravity #302 owns `services/discovery/sources/**` and Dice tests; Ollama/OpenCode #318 owns `services/ats_detection/**` and detector tests. Avoid both. Next, finalize the evidence/lane checkpoint, fetch current main, reconcile all claims, acquire integration lock, update canonical acceptance records without shrinking 366 requirements, integrate if still a safe fast-forward, release claims/lock, and continue.


## Active issue #96 checkpoint — 2026-10-07T12:18:49Z

Project and accomplishment rewrite slice is committed/pushed at source `97905378cdc79e437a7cae2fe2fbb7c504d0affe` from base `911f248db3110f095c9da98a452bab8797aeda16` in isolated worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req095-reviewed-achievement-rewrites-20261007`. Red test reproduced unsupported fact kinds. Unit, integration and connected Chromium acceptance passed; full Python/browser suite passed 908 tests with 5 skips; frontend 63/build and local validators passed. Evidence `docs/evidence/2026-10-07-resume-reviewed-achievement-rewrites.json` is being recorded. Issue #96 remains OPEN/PARTIAL. External submission OFF; hosted checks/review NOT_RUN_BY_USER_POLICY.


## Issue #96 released — 2026-10-07T12:20:17Z

Integrated at main `cc9fc3832f47e45cb9e60615c56b430692c3c614` from source `97905378cdc79e437a7cae2fe2fbb7c504d0affe` with evidence `docs/evidence/2026-10-07-resume-reviewed-achievement-rewrites.json`. GitHub release readback https://github.com/MulattoTech/MJobs/issues/96#issuecomment-6037769866. User-reviewed project/accomplishment rewrites preserve source provenance/history; full Python/browser 908 PASS, 5 skipped; frontend 63/build PASS. Requirement remains OPEN/PARTIAL. Claim released; continue from fresh reconciliation.

## Active issue #181 checkpoint — 2026-10-07T12:26:56Z

Issue #181 / MJ-REQ-180 is claimed in machine and GitHub coordination. Source commit `bb839a547403a9991bd8c0dfd6357649d1bb7775` is pushed from base `7d46e45c7139c0be356c163956d6a79ad4010eb3` in isolated worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req180-redacted-visual-feedback-20261007`. The live-feedback opt-in now writes a final screenshot with all standard form controls and `data-sensitive` regions masked; raw Playwright DOM traces/snapshots are removed. Focused unit/adjacent GUI/Chromium tests: 21 PASS; compile and local validators pass. One unrelated existing GUI test emitted a transient Windows connection-aborted log while passing. Evidence: `docs/evidence/2026-10-07-redacted-visual-feedback.json`. Requirement remains OPEN/PARTIAL because generic free-form rendered text and the separate diagnostic-text option can still carry private data. Keep external submission OFF and hosted validation NOT_RUN_BY_USER_POLICY. Integrated at `9b51b1d7a7d5f94d1e06412439edde5250d6ba5e`; claim release readback https://github.com/MulattoTech/MJobs/issues/181#issuecomment-6037942896. Continue after fresh reconciliation.

## Issue #181 released — 2026-10-07T12:30:39Z

Integrated at synchronized main `9b51b1d7a7d5f94d1e06412439edde5250d6ba5e`; GitHub release readback https://github.com/MulattoTech/MJobs/issues/181#issuecomment-6037942896. The scoped visual capture behavior is accepted locally; full MJ-REQ-180 remains OPEN/PARTIAL. Hosted checks/review NOT_RUN_BY_USER_POLICY; external submission OFF. Codex issue claim released; reconcile all lanes before the next selection.

## Active issue #171 checkpoint — 2026-10-07T12:35:48Z

Issue #171 / MJ-REQ-170 live-feedback retention is claimed in machine and GitHub coordination. Source commit `e053916c4a5d6e3606e138feefb4e86823631677` is pushed from base `9a6612e48040d2e17af7ba13a579fc66521a5fea` in isolated worktree `C:/Tools/Dev/MJobs/.worktrees/codex-mj-req170-feedback-retention-20261007`. New sessions now prune only recognized direct-child feedback directories older than 30 days, preserve the cutoff boundary and application history, and repair `LATEST.txt`. Exact-head feedback unit/adjacent browser checks: 24 PASS; compile and repository validators pass. Evidence: `docs/evidence/2026-10-07-live-feedback-retention.json`. Broader MJ-REQ-170 remains PARTIAL. Integrate under lock, release the claim, then reconcile and immediately continue.


## Issue #171 released — 2026-10-07T12:39:55Z

Issue #171 / MJ-REQ-170 is integrated PARTIAL at synchronized main `64dfebcd51d0d1c926d9b3856a42028ec577f88d`. Release readback: https://github.com/MulattoTech/MJobs/issues/171#issuecomment-6038062799. Machine claim released; focused acceptance was 24 PASS and hosted checks remain NOT_RUN_BY_USER_POLICY. The retention feature covers recognized live-feedback session directories only; broader retention requirements remain open. Reconcile current main, active lane claims and dirty worktrees, then select the next unclaimed dependency-ready issue. External submission OFF.


## Active issue #181 diagnostic privacy — 2026-10-07T12:47:41Z

Issue #181 / MJ-REQ-180 is active on branch `codex/mj-req180-diagnostic-privacy-20261007` from main `a2053c432d618d49af3e5509a6de00fc0c718392`. Source commit `f557cd02878b234170470b88ff4f916917a97a25` is pushed (tree `a5fbe668101b47da34d0ac4270b95bf475cb6003`). Raw console/page-error strings are no longer stored even with the legacy diagnostic flag; recognizable common sensitive patterns are redacted from metadata and masked in optional screenshots. Focused feedback unit/GUI/Chromium tests: 26 PASS. Evidence: `docs/evidence/2026-10-07-redacted-diagnostic-privacy.json`. Issue remains OPEN/PARTIAL for unclassifiable values, image/canvas text and debug surfaces outside this recorder. GitHub claim update: https://github.com/MulattoTech/MJobs/issues/181#issuecomment-6038209994. A zero-byte untracked `events.jsonl` appeared during focused GUI tests and is intentionally preserved outside Git. Hosted checks NOT_RUN_BY_USER_POLICY; external submission OFF.


## Issue #181 diagnostic privacy released — 2026-10-07T12:49:10Z

Integrated PARTIAL at synchronized main `04507e36bd78b6af4053868f00926d2b8b993b0f`; release readback https://github.com/MulattoTech/MJobs/issues/181#issuecomment-6038249056. Claim released. Focused feedback test set 26 PASS; hosted checks NOT_RUN_BY_USER_POLICY. Full MJ-REQ-180 remains incomplete for unclassifiable values and out-of-scope debug surfaces. Continue from fresh live reconciliation.


## Active issue #8 migration recovery — 2026-10-07T12:57:51Z

Issue #8 / MJ-REQ-007 is active on `codex/mj-req007-migration-recovery-20261007` from main `82b56526f041cdb2e456dcd7c29ad680011da9dd`. Source/test commit `7a2107941a4f3ac0379f0053bd1f0421db117f80` (tree `f89ef1004ac2448f7a2fe7b099e97f6e0291f8e3`) is pushed. Reproduced an interrupted SQLite migration leaving an orphan table while `alembic_version` remained at parent; online migrations now explicitly start SQLite transactions. The same injected DDL failure rolls back and retries successfully on temporary SQLite and isolated PostgreSQL 17. Existing migration integration tests: 23 PASS, one optional PostgreSQL skip; related init-env/backup units: 18 PASS. Evidence `docs/evidence/2026-10-07-migration-recovery.json`. A separate offline `--sql` attempt fails in existing revision `cb72f9041d6a` because it queries a live connection; protected-config migration recovery is also unverified. Claim active; do not modify canonical planning outside lock. External submission OFF; hosted checks NOT_RUN_BY_USER_POLICY.


Issue #9 / MJ-REQ-008 checkpoint (2026-10-07T13:05:44Z): source `9c4dfacdeb5f65883284c7d7628b653097546b12` on branch `codex/mj-req008-postgres-worker-recovery-20261007` adds loopback-only disposable PostgreSQL process-crash recovery, attempt reclaim, durable audit readback, and stale-worker fencing tests. Worker lifecycle module 18 PASS (2 PostgreSQL, 16 SQLite); PostgreSQL concurrency tests 5 PASS; validators PASS. Evidence `docs/evidence/2026-10-07-postgres-worker-crash-recovery.json`. One first run exposed test residue at the concurrency ceiling; per-test cleanup was added and the passing run used a fresh auto-removed PostgreSQL 17 container. Host restart and full-suite checks remain NOT_RUN; issue stays OPEN/PARTIAL. Claim: https://github.com/MulattoTech/MJobs/issues/9#issuecomment-6038481363


Issue #5 / MJ-REQ-004 checkpoint (2026-10-07T13:13:02Z): source `0a14aaa2d9c0fe2cb1ae0c94a3281a1ebd99987f` replaces unbounded Compose recovery stdout/stderr capture with a 2 MiB bounded stdout runner, drained stderr, strict timeout, child terminate/reap, and fixed sanitized errors. Both `docker compose` and recovery-worker `docker inspect` use the runner. Pre-fix red reproduced returning 2 MiB + 1 bytes without refusal; focused 4 PASS, all 408 unit tests PASS, validators PASS. Evidence `docs/evidence/2026-10-07-compose-recovery-output-bounds.json`. Live Compose recovery intentionally NOT_RUN to avoid stopping/restarting local services; issue remains OPEN/PARTIAL. Claim: https://github.com/MulattoTech/MJobs/issues/5#issuecomment-6038614779.


## Active issue #180 / MJ-REQ-179 checkpoint — 2026-10-07T16:01:18Z

Branch: `codex/mj-req179-safe-browser-debug-capture-20261007`; worktree: `.worktrees/codex-mj-req179-safe-debug-capture-20261007`; base main: `bd92e070dba13513e78e6127664417c5082fd337`; source: `f8d07ca9a83e9f61541ad74f2bcb1c1c46f02165`; tree: `9f09bf53f637aff637be8f8c4188667c050cf167`.

The local mock browser failure capture is implemented and pushed on f8d07ca9a83e9f61541ad74f2bcb1c1c46f02165 (tree 9f09bf53f637aff637be8f8c4188667c050cf167). Exact-head targeted validation: 73 tests, 72 PASS, 1 optional PostgreSQL skip. Compose default-off/opt-in, redacted screenshot and structural DOM, sanitized URL, error-only console/network fingerprints, 30-day/20-attempt retention, REVIEW_REQUIRED and SUBMISSION_UNCONFIRMED/no-blind-retry behavior are covered. Validators pass. Repository-wide discovery at prior source commit failed (948 run, 2 failures, 9 errors, 28 skipped) and is not called PASS. Real ATS adapters/live PostgreSQL/hosted checks were NOT_RUN; external submission OFF. Evidence: docs/evidence/2026-10-07-safe-browser-debug-capture.json.

Focused acceptance oracle is the synthetic local mock browser flow: capture is opt-in; structural DOM and screenshot are redacted; error state persists; ambiguous submission does not retry. Exact-head focused suites: 73 run, 72 PASS, one optional PostgreSQL skip. Exact-head agent_skills, catalogue, progress, lane coordination and diff checks PASS. Full discovery at prior source: 948 run, 2 failures, 9 errors, 28 skipped; output was truncated by terminal transport and no attribution is claimed.

GitHub Actions/review NOT_RUN_BY_USER_POLICY; live PostgreSQL and real ATS NOT_RUN; external submission OFF. Preserve untracked `events.jsonl` in the worktree. Next: acquire integration lock, reconcile and fast-forward only if main remains ancestor; then release claim and select next unclaimed dependency-ready issue.


## 2026-10-07 Codex issue #174 closeout

- Requirement MJ-REQ-173 PASS and GitHub issue CLOSED. Candidate 84136dcabc99f831538b66f2450292152336750b was integrated by non-force fast-forward from 5be79530d5d669c5c2d6a00c38f37afe120ff16b; canonical closeout 0770961b24295e021f8e78084a11406628c84958.
- Evidence docs/evidence/2026-10-07-seven-kind-upload-security.json; acceptance https://github.com/MulattoTech/MJobs/issues/174#issuecomment-6045710354; AI LANE RELEASED https://github.com/MulattoTech/MJobs/issues/174#issuecomment-6045730683. Local claim released. 16 affected Python integration/browser tests, frontend 63/build, SQLite migration preservation and local validators pass. Hosted checks NOT_RUN_BY_USER_POLICY; PostgreSQL/Docker NOT_APPLICABLE; external submission OFF.
- Resume by reconciling current main, active claims and dirty active worktrees, then continue with the highest-value unclaimed dependency-ready issue.


## 2026-10-07 Issue #177 search-profile settings audit closeout

The search-profile SETTING_CHANGED slice was integrated at d4fd68c73acfeab3c47d523ac2de822c0c1e841f from source 96ccfc0e6b0c1ab095b41f1337579820b118d86f. Effective updates to existing profiles write one generic empty-payload audit event in the same transaction; initial creation and unchanged saves emit none. Evidence: docs/evidence/2026-10-07-search-profile-audit-history.json. Targeted integration/browser tests: 10 PASS; full Python: 966 PASS, 8 skipped; frontend: 63 PASS and production build PASS; local validators PASS. GitHub comment: https://github.com/MulattoTech/MJobs/issues/177#issuecomment-6047708003. Issue #177 remains OPEN/PARTIAL for remaining settings and truthful event lifecycle coverage. Hosted tests/review NOT_RUN_BY_USER_POLICY; PostgreSQL/Docker NOT_RUN; external submission OFF. The source worktree contains an unrelated zero-byte untracked events.jsonl; preserve it.


## 2026-10-07 Issue #177 AI cost-policy audit slice

Source 612a697b47dbf9a9c695261835727445dd699b1b, tree 4ce56334dcdd71470a11ab682aa738032c0c35d6, evidence docs/evidence/2026-10-07-ai-cost-policy-audit.json. Effective profile AI cost-policy saves now append one generic empty-payload SETTING_CHANGED row atomically; absent-row defaults and unchanged saves emit none. 35 affected integration/browser tests, full Python 967/8 skipped, frontend 63/build and local validators PASS. Claim: https://github.com/MulattoTech/MJobs/issues/177#issuecomment-6047753340. Pending integration lock and fast-forward; keep issue OPEN/PARTIAL. PostgreSQL/Docker NOT_RUN; hosted NOT_RUN_BY_USER_POLICY; external submission OFF. Preserve the unrelated zero-byte events.jsonl in the worktree.


## 2026-10-07 Issue #177 AI cost-policy audit closeout

Source 612a697b47dbf9a9c695261835727445dd699b1b, validated tree 4ce56334dcdd71470a11ab682aa738032c0c35d6, and evidence docs/evidence/2026-10-07-ai-cost-policy-audit.json integrated at main 2d5dcaba6b20012df93de8546f216ecdd5a26d3c. The policy write and one generic empty-payload SETTING_CHANGED record commit together only for effective changes; default-equivalent and unchanged saves are silent. 35 affected integration/browser tests, full Python 967/8 skipped, frontend 63/build, and local validators PASS. GitHub integration comment: https://github.com/MulattoTech/MJobs/issues/177#issuecomment-6048007349. Issue #177 remains OPEN/PARTIAL. Hosted NOT_RUN_BY_USER_POLICY; PostgreSQL/Docker NOT_RUN; external submission OFF. Preserve the zero-byte events.jsonl in the source worktree.


## 2026-10-07 Issue #177 company-source settings audit closeout

Source implementation `0c76b225f48a882e8aa90b5e7686d22a8891425e` (tree `3d7733e26c5d4d097518d3c911d6029c7f1c5e86`) and evidence `docs/evidence/2026-10-07-company-source-setting-audit.json` were integrated by non-force fast-forward at synchronized local/GitHub main `68a0630b690cfa855e43829bc4f1836cf6add246` from `c0ea57bca0e4a7c37b71f19d9cdd07bb062113a1`. Effective company-source pin and enabled changes append one generic empty-payload `SETTING_CHANGED` event for each profile with a recorded enrollment link, in the setting transaction. No-op and unlinked-source changes add no profile event; existing source-global audit entries remain.

Validation: 21 focused integration tests, 2 connected Chromium tests, frontend 63/build, full Python 968 run/8 skipped, agent-skills/catalogue/progress/lane validation, compileall and diff checks PASS. PostgreSQL/Docker NOT_RUN; hosted tests/review NOT_RUN_BY_USER_POLICY; external submission OFF. GitHub checkpoint: https://github.com/MulattoTech/MJobs/issues/177#issuecomment-6048627184; claim released: https://github.com/MulattoTech/MJobs/issues/177#issuecomment-6048641183. Issue #177 remains OPEN/PARTIAL for other settings and complete truthful lifecycle audit coverage. Preserve the untracked zero-byte `events.jsonl` in the feature worktree.


## Active issue #517 structured source-review evaluation checkpoint — 2026-10-08T04:15:38Z

Branch `codex/mj517-structured-source-review-20261007` is isolated from base main `3476f4c8b4594f18dc21f61e9ee7fce165da5826`. The scoped feature/test source tree is `bea4c99bca9e3194e105a717b58dd472432ca8d4`. Review events now preserve source kind, explicit API/feed evidence status and a sanitized public reference, evidence/access/capability/value notes, and profile-scoped history. Sources UI writes and reads these fields after reload. Collection remains disabled; no external request is issued. Evidence: `docs/evidence/2026-10-07-source-candidate-review-evaluation.json`.

Validation: source-candidate integration/browser tests 7 PASS; frontend 63 PASS and TypeScript/Vite build PASS; agent-skills/catalogue/progress validators, compileall, diff check, and isolated SQLite migration upgrade/backfill/downgrade-preservation check PASS. PostgreSQL/Docker and live source access NOT_RUN; hosted checks/review NOT_RUN_BY_USER_POLICY; external submission OFF. The review is attributable to profile, event, and timestamp; the existing app has no authenticated named reviewer principal. Issue #517 remains OPEN/PARTIAL for provider/browser capture coverage and integration task linkage. The local claim remains active pending integrator reconciliation. GitHub claim: https://github.com/MulattoTech/MJobs/issues/517#issuecomment-6052010620.

## Active issue #116 / MJ-REQ-115 checkpoint — 2026-10-08

Branch `codex/mj-req115-certification-strength-20261007`, worktree `.worktrees/codex-mj-req115-certification-strength-20261007`, based on `b94999bb66537eb2b28df26e648db96e36d0d6b2`. The certification parser now returns `UNKNOWN` when the clause does not state a strength. Manual capture persists parsed certification mentions in job attributes, and connected Jobs detail displays factual strength labels with an explanation for UNKNOWN. Issue #116 remains OPEN/PARTIAL; this slice does not close the broader MJ-REQ-112..119 scope.

Evidence: `docs/evidence/2026-10-07-certification-requirement-strength.json`. Focused parser/integration tests 15 PASS; synthetic connected Chromium manual-capture test 1 PASS including post-reload readback; frontend 63 PASS; TypeScript/Vite build and agent-skills/catalogue/progress/lane/compileall/diff checks PASS. Full unittest discovery completed with 971 tests, 59 errors and 8 skips; the visible errors identify Alembic `MultipleHeads` for `9b6d40aaf163` and `f37a0c2e91b5` when tests request `upgrade(..., 'head')`. This broad result is PARTIAL and not attributed to this slice. Hosted checks/review NOT_RUN_BY_USER_POLICY; PostgreSQL/live providers NOT_RUN; external submission OFF.

Claim: https://github.com/MulattoTech/MJobs/issues/116#issuecomment-6052160980; scope update: https://github.com/MulattoTech/MJobs/issues/116#issuecomment-6052165364. Do not integrate or publish from this lane. Preserve the untracked `events.jsonl` produced in the local worktree by test execution; it was excluded from the candidate commit.
