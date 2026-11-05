# PR #932 — independent resume provenance review and boundary regressions

Run: `web-20261105T000108Z-2cecb11f-ba3e-41b2-bf82-09d09e616213`.
Scope: MJ-TH-014 / epic #904; no whole-feature or original-requirement acceptance promotion.

## Source and ownership

Reviewed original source `cfef77aa06b8b5e6836d9a7d8e53d58238939eba` independently of its authoring session. The profile ID, saved upload ID and SHA-256 guard, latest durable extraction selection, manual older-version selection, generation invalidation and apply/dismiss paths were inspected. No new blocking issue was found in that narrow source diff. Existing public independent review: <https://github.com/MulattoTech/MJobs/pull/932#issuecomment-6624932584>.

Atomic objective ref `refs/heads/webpro-claims/PR-932-INTEGRATION` was created with HTTP 201 and verified at owner `67a59a420c28968667ac986079c61646c945f2bd` at `2026-11-05T00:04:04.3477884Z`. Starting main was `42ba63525c93e26bfe7168c4875a21fee4a5c6cd`. Claim commit is not product history.

Added regression source commit: `28c81e0128451e8ff8bda173436ab0a22440835e`. This run changes only tests and this evidence, preserving the original production patch.

## Executed verification

The following ran in a private Windows worktree with its own frozen npm dependency installation:

- `npm --prefix apps/web test -- src/resume-library.test.tsx src/profile-import.test.ts`: **13 PASS**, no skipped cases (8 component cases and 5 import-mapping cases).
- `npm --prefix apps/web run typecheck`: **PASS**.
- `npm --prefix apps/web run lint`: **PASS**, 297 files.
- `git diff --check`: **PASS**.
- `python -m tools.verification.commit_provenance --commit HEAD --strict`: **PASS**, one authored test source.

Six additional component cases exercise foreign-profile saved metadata, selected suggestions after removal/replacement/profile change, a late old-profile extraction response, and explicit selection/apply with review cleanup. They use synthetic data and mocked API responses; they are not a live browser or backend acceptance claim.

A separate red control temporarily used the exact pre-fix `resume-library.tsx` from `45fe33e6fd06acbadaf5d1199a7ce168d40c4baf` in this owned worktree. `npm --prefix apps/web test -- src/resume-library.test.tsx -t 'withholds review and apply'` failed as expected: the foreign-profile extraction review and apply action were visible. Result: **1 FAIL / 7 unselected SKIP**. The candidate production file was restored in a `finally` block, with blob `df40e0caf718bf754630eca44475bb5205e255aa` verified. The failure is retained as regression evidence, not counted as a passing test.

Installed Node was `v27.0.0-alpha.0.0.0`; npm emitted a compatibility warning. The red-only run also emitted React act-environment warnings. No global Node/npm dependencies or other worktrees were modified.

## Remaining gates and delivery

Original PR head had successful required CI, but it was behind current main. New/current-base candidate tests, exact-revision required CI, strict protected integration and guarded production deployment are separate gates. Their final hashes and results will be recorded in the PR conversation after completion; they are **NOT VERIFIED** by this pre-publication note.

No canonical matrix, Project/Fleet acceptance, private resume, production database, external application, paid model, fleet cutoff, Cloudflare route or production service was changed. No new heavy resource slot was used for these focused tests. Rollback remains a protected revert of the original scoped production change; the added tests introduce no runtime data migration.
