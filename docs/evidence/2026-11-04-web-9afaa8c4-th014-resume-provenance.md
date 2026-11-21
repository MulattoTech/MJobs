# MJ-TH-014 — source-attributed base resume extraction review (2026-11-04)

- Run: `web-20261104T061553Z-9afaa8c4-7874-4f16-8665-380964d13278`
- Exact objective claim: `refs/heads/webpro-claims/MJ-TH-014` → `6b03ee5742455c7dc1e94e1af1d4ebc2cc7eca5c` (created atomically, matching read-back)
- Baseline: `main` `45fe33e6fd06acbadaf5d1199a7ce168d40c4baf`
- Branch: `work/web-20261104T061553Z-9afaa8c4-th014`
- Scope: `apps/web/src/resume-library.tsx` and new `apps/web/src/resume-library.test.tsx`

## Reproduced gap

Two synthetic, deterministic Vitest cases were added to the unchanged source. Both failed because the extraction review omitted the saved resume collection/version/filename and did not bind visible suggestions to a particular original upload:
- Latest completed durable extraction among two saved versions.
- Explicit extraction of an older version after the latest was previously reviewed.

No real applicant documents or external services were used.

## Correction

The review now displays the exact saved collection label, version and filename from its authoritative `Resume` record. A result is bound to profile ID, upload ID and file SHA-256. If that source is absent or mismatched, the review and apply action are withheld with a reload notice. Binding is renewed after a completed durable readback or manual extraction, and cleared on scope change, new upload, new extraction, apply, or dismissal. Imported facts remain unverified until the existing user approval flow.

## Executed checks (isolated worktree)

- Before source change: `npm --prefix apps/web test -- src/resume-library.test.tsx` — **2 FAIL** (expected red; 50 ms test duration).
- After source change: same Vitest target — **2 PASS**, 0 FAIL (538 ms whole run).
- `npm --prefix apps/web run typecheck` — **PASS**.
- `npm --prefix apps/web run lint` — **PASS**, 297 files.
- `npm --prefix apps/web run build` — **PASS**, 139 modules transformed. Existing bundle-size warning above 500 KiB remains.
- `git diff --check` — **PASS** on source; staged diff must also pass before PR.
- Hosted CI, browser/mobile E2E, production behavior, and guarded deployment — **NOT RUN** at local source checkpoint.

## Limits and recovery

This is a review-stage provenance and stale-source safety slice, not completion of full résumé versioning, private-source trace through final profile save, or all TalentHop parity criteria. No original P0/P1 acceptance change, employer application, live data mutation, Cloudflare change, paid AI call, or external upload was performed. Revert the scoped commit to remove the UI behavior; no migration or service restart is needed. All source tests use synthetic filenames and statuses only.
