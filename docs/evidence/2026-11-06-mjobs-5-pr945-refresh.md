# Current publication disposition — mechanical refresh, runtime failure unresolved

Primary mjobs-15 authorized publication of the mechanical current-main refresh with the unresolved local failure disclosed. This is NOT a runtime repair or acceptance clearance. Merge remains held pending explicit disposition of the failure and new-head gates.

Base main:19dd2cdb92b71f531f35a07f68aff339b8fcd69a. Local parent:c913b5f5f67de356fa534135819ce4d44f276025, which preserves published PR9453bbece377bbdb4c15dd597802187ece9f3fc244e and PR95091fe01ac51c2033f87743f5db326972bcbb1ec2b. Mechanical merged executable tree before this evidence update:8f52e1b279d18c38bd584b23b70a48f82022edf9. Final commit/tree are the Git identities containing this note; only this evidence file differs from that executable tree.

All eight PR-owned executable/test blobs are identical to reviewed remote3bbece377bbdb4c15dd597802187ece9f3fc244e. The other original evidence files remain unchanged. Main contributes API/Store, web salary wiring, selected-search, worker cancellation and collector/ATS changes through6a47f562, then source-candidate catalogue/test changes through19dd2cdb. They are inherited main changes, not edits made by this owner. No manual conflict resolution, new product code or new tests.

## Actual candidate validation and failure

At c913b5f5f67de356fa534135819ce4d44f276025 / tree10da53116a9faef64963ebfcd97ee0c8a1115d19 on Windows11 build26300, Python3.12.10, Node27 alpha, with owned-checkout imports verified:

- Existing backend modules test_pipeline_workbench unit/integration and test_reviewed_cover_letter_prepare_binding:52 PASS +41 subtests PASS,57.83s,zero skips.
- Existing focused pipeline-workbench frontend test:11 PASS,584ms.
- Typecheck/production build:PASS,140 modules; existing large-chunk warning.
- Original connected Chromium run:FAIL,14.29s. tests/browser/test_pipeline_workbench.py:164 asserted the API server thread must stop after the unchanged10-second join; it remained alive. Logs included asyncio Proactor socket.shutdown WinError10054. Functional completion in that run is UNKNOWN because finally could mask an earlier exception; its trailing external-request guard was not reached. Windows10054 alone is not causal proof.

A separately authorized diagnostic-only edit preserved original and teardown exceptions, recorded bounded value-free lifecycle stages/counts/stacks, and retained the ordinary shutdown/10-second join and all assertions. Exactly ONE diagnostic run:1 PASS in8.19s. All closed/reloaded/no-queue/reopen/queue/isolation/privacy/page-error/external-request markers completed; shutdown returned and thread exited. Entry snapshot:0 connections,2 request tasks,0 active transports. No10054 or failure recurrence. This result is NONDISCRIMINATING, does not establish the shutdown-before-detach hypothesis, and does not erase the original FAIL.

The possible mechanism remains unconfirmed: installed Python3.12.10 can raise during socket.shutdown before transport detachment, potentially leaving asyncio Server.wait_closed pending. The failed run retained no task/count snapshot proving that path. No dependency patch, timeout relaxation, private-state mutation, force shutdown, speculative product repair, or further browser attempt was made.

## Diagnostic recovery and removal

Before removing only this owner's diagnostic hunks, an exact file copy and binary Git patch were saved in system temp and retained alongside the sanitized probe log and retained original-failure excerpt. Absolute workstation paths are deliberately omitted here.

- Recovery directory name:mjobs-5-pr945-diagnostic-recovery; files:diagnostic.patch and test_pipeline_workbench.py.
- Diagnostic file Git blob:04a034e46e1952a989b80d81bb1ced6e00c21285.
- File SHA256:c07346f390486b2e3938527cfda00ce870fc4c07d9b2a2e179a9547f862506d1.
- Patch SHA256:7b552a8afac91781088baf3c0e738773dfd19db75ce47fdf6da20b4d15ba70e7.
- Sanitized one-run log SHA256:3ce6335aa7b50082d6737543bf6b8a436f02ab804cebb7900dfce3fb39f3e1f7.
- Original-failure excerpt/diagnosis SHA256:ad1025d09daecf12eb4e139f687f2da253cf95e2e3c887d9d13561411ce32893. This is a retained excerpt, not complete raw original stdout/stderr.

Exact-copy blob verification and reverse-patch check passed before removal; forward-patch applicability passed afterward. Browser test now matches the published original blob. Recovery material is local, not published; no unrelated work/history was reset, stashed or removed.

## Current gates

Runtime/backend/frontend/browser checks on the new19dd2cdb-based head:NOT_RUN. Prior receipts below and above are bound to their recorded revisions only. Static owned-blob comparison and whitespace checks passed. New exact-head hosted CI/native review must be observed separately; neither authorizes merge or clears the unresolved local failure. Both parent requirements remain PARTIAL. No main edits, acceptance matrices, real applications, deployment or integration.

---
# PR945 current-main refresh — mjobs-5

UTC: 2026-11-06. Requirements: MJ-REQ-171 / issue172 and frozen MJ-REQ-155 / issue156 component.

## Candidate and preserved authorship

Registered isolated worktree branch: ao/security-triage/pr945-refresh. Existing PR945 head868f4e11ec5139c33dfda87aea0313092d689134 is the first parent; current main560b97eda0fcb0b80f4b63e8fe5b28e805414580 is merged without rewriting history. PR950 head91fe01ac51c2033f87743f5db326972bcbb1ec2b remains an ancestor. No change to PR950.

The merge was conflict-free. All eight assigned source/test files are byte-identical to previous PR945, and both PR950 source/test files plus its original evidence are byte-identical to PR950. NO_NEW_GAP: existing candidate already implements strict application-ID/pre-resolve alias rejection and closed-job readiness. No new feature, source correction, duplicate test, dependency or migration was introduced. The only new authored file is this evidence/handoff.

Exact tested merged tree before this evidence file: 670f0b449ec7080f7ac323c54c21ec3ee2adc1dd. All executable/test/config inputs remain identical after this evidence-only addition. Merge commit identity is available from the commit containing this file; no circular self-hash claim.

## Executed verification

Windows, Python3.12.10, Node27.0.0-alpha.0.0.0. Existing Python environment reused; explicit import assertions confirmed services/documents/reviewed_cover_letter.py and services/operations/pipeline_workbench.py were loaded from this registered worktree. npm dependencies installed locally from cache with `npm --prefix apps/web ci --offline --ignore-scripts --no-audit --no-fund`; no lockfile changes.

- `python -B -m pytest -q -p no:cacheprovider tests/unit/test_pipeline_workbench.py tests/integration/test_pipeline_workbench.py tests/integration/test_reviewed_cover_letter_prepare_binding.py --tb=short`: **52 passed,41 subtests passed,64.68s**, no failures/skips.
- `npm --prefix apps/web test -- src/pipeline-workbench.test.tsx`: **11 passed**. This is the existing focused test, not a whole-frontend coverage claim.
- `npm --prefix apps/web run build`: typecheck and production build **PASS**,139 modules. Existing oversized-chunk warning remains.
- `python -B -m pytest -q -p no:cacheprovider tests/browser/test_pipeline_workbench.py --tb=short`: **1 passed,8.74s**, no skips. Existing isolated SQLite/API/Chromium fixture blocks external requests and exercises closed/reloaded refusal, reopening, explicit queue and profile isolation.
- `python -B -m tools.verification.catalogue`: PASS.
- `python -B -m tools.verification.progress validate`: PASS.
- `python -B -m tools.verification.progress_dashboard --check`: CURRENT.
- `git diff --cached --check`: PASS; no unstaged tracked or untracked changes before adding this note.

Tests ran serially in one authorized heavy slot. Local synthetic checks do not certify a local filesystem race attacker, production behavior, full parent requirements, or hosted CI. No live applicant data, employer submission, deployment, host changes, or paid scans.

## Publication handoff

AO claim attaches existing PR945 to mjobs-5; primary mjobs-1 retains integration authority. Prior objective refs were absent at intake. Original source history/reviews remain attributed to original authors/executors. New exact-head hosted checks and AO review are NOT_RUN at this checkpoint. Report stable commit/tests to primary before serialized publication; do not merge. Parent requirements remain PARTIAL. No global acceptance, queue, or shared handoff mutation.

Next: primary-authorized ordinary update to existing PR945, then exact-head required checks/review. Preserve source branch ancestry and no-force history. Rollback is a protected revert if later integrated; no schema/data migration is needed.
