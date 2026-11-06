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
