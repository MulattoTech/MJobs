# MJ-TH-015 — bounded recently viewed jobs (candidate evidence)

Run: `web-20261104T061508Z-1a17193c-c16f-476d-8a00-679c8559c9c0`
Issue: https://github.com/MulattoTech/MJobs/issues/919
Owner ref: `refs/heads/webpro-claims/MJ-TH-015` at `37cbb7a3d8a050e0bcc64c2a03c2777edfbd3445`
Baseline: `main` `45fe33e6fd06acbadaf5d1199a7ce168d40c4baf`
Branch: `work/web-20261104T061508Z-1a17193c-recent`

## Behavior

- A successful explicit job-detail open (with an active profile) records a visit via a dedicated POST. Plain GETs, search runs and application tracking do not record visits.
- The new `profile_job_views` table stores one mutable row per profile/job, ordered by serialized visit sequence. New opens keep at most 100 rows per profile, pruning visits older than 90 days.
- GET includes only visits from the previous 90 days; an orphaned/removed job is reported unavailable rather than guessed. Older physical rows remaining after inactivity are purged on the next recorded visit; this is **not** a continuous background deletion guarantee.
- Profile-scoped DELETE clears visit rows, without touching job dispositions, audit notes, application state or search-run history. The web UI requires a confirmation.
- Jobs UI includes a separate Recently Viewed panel with refresh, retry, clear, stale-job explanation and an accessible note that saved-only filtering is not available here.
- Migration is additive. Downgrade refuses to discard nonempty private visit history.

## Local checks (before protected CI)

- `python -m pytest -q -p no:cacheprovider tests/integration/test_recently_viewed.py --tb=short`: **4 PASS**.
- `python -m pytest -q -p no:cacheprovider tests/integration/test_recently_viewed_migration.py --tb=short`: **2 PASS**.
- `npm --prefix apps/web test -- src/recently-viewed-client.test.ts src/recently-viewed-panel.test.tsx`: **6 PASS** (including after UI changes).
- `npm --prefix apps/web run typecheck`: **PASS** (including after UI changes).
- `npm --prefix apps/web run lint`: **PASS**, 300 files (pre-final minor text/guard edits).
- `npm --prefix apps/web run build`: **PASS**, 141 modules (pre-final minor text/guard edits); Vite warns about an existing >500 kB bundle.
- `python -m alembic heads`: **20261104rv01** is the single head.
- `git diff --check`: **PASS** on tracked changes before final verification.

The combined existing-integration regression command was started but its terminal result was not captured by the remote session tool; it must not be counted PASS without another verified result.

## Boundaries and next evidence

- Tests use synthetic FIXTURE profiles/jobs only; no TalentHop account, third-party API, real employer application or paid model was used.
- Main-branch CI, required review, migration rehearsal against a production-like backup, guarded deployment, live/browser E2E and acceptance crosswalk are **NOT RUN** until the exact PR candidate is reviewed.
- This change does not update the original P0/P1 acceptance denominator or claim MJ-TH-015 ACCEPTED. Verify protected checks, migration/rollback safety and deployed user journey before changing tracking.
- Existing app-level profile selection and Cloudflare Access policy remain unchanged; this slice does not add multi-tenant identity controls.
