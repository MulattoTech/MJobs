# MJ-REQ-264 — parked company-discovery profile revision renewal

- Run: `web-20261104061509-3ae27784-784`
- Owner claim: `refs/heads/webpro-claims/MJ-REQ-264` → `9c94f1767efd37162d44c11e0979935a74272333`
- Baseline protected main: `45fe33e6fd06acbadaf5d1199a7ce168d40c4baf`
- Branch: `work/web-20261104061509-3ae27784-784-264`
- Requirement: MJ-REQ-264, issue #265. **Whole criterion remains PARTIAL.**

## Reproduced against unmodified request source

Two synthetic SQLite integration cases failed on the pre-change code:

1. A still-parked `NEEDS_ORIGIN_REVIEW` request retained the old profile version after an eligible intake from the same profile's new saved revision.
2. Another saved profile could add `USER_PREFERRED_COMPANY` to the first profile's coalesced company/source request.

Initial test setup used a repeated collector call and hit the expected `SOURCE_COOLDOWN`; that harness error was corrected by directly exercising the transactional request entry point. The final baseline red reproduction was **2 FAIL in 2.62 s**, attributable to the two behaviors above. A separate follow-up red case against the intermediate fix proved that a stale profile could insert a *new* request before a post-insert rejection: **1 FAIL in 2.08 s** (expected 0 rows, observed 1). A pre-insert saved-version guard corrected it.

## Changed behavior

- Reject foreign-profile or stale-profile intake without changing a preexisting request's reasons, score, revision or authority. Saved-profile freshness is checked **before** the upsert to avoid creating stale intents; a changed version detected after the row lock raises a transaction-rollback error. No other applicant profile is silently bound to it.
- For a different revision of the same current saved profile, renew **only** an unreviewed, operation-free, origin-free `NEEDS_ORIGIN_REVIEW` intent with unchanged source snapshot.
- Re-read current selected job/candidate with a PostgreSQL `FOR KEY SHARE SKIP LOCKED` fence before altering the trigger-job foreign key; defer if a collector owns the job row.
- Revalidate existing company/source/candidate provenance through `_source` before updating. Replace stale profile-specific score/reasons, increment the request revision exactly once, and audit the old/new binding in the same transaction.
- With an unchanged source snapshot, profile-only updates leave queued, running, terminal, dismissed, reviewed and authorized work bound to its original profile/operation. The separate pre-existing source-configuration renewal path is unchanged. No worker replay or new employer authority is created.

## Executed source verification

- `python -m pytest -q -p no:cacheprovider tests/integration/test_company_discovery_policy.py` — **14 PASS, 22 subtests PASS, 16 PostgreSQL-only SKIP**, 8.93 s (before the additional negative/rollback/locking tests).
- `python -m pytest -q -p no:cacheprovider tests/integration/test_company_discovery_policy.py tests/integration/test_company_discovery_idempotency.py tests/integration/test_company_discovery.py tests/integration/test_company_discovery_requests_migration.py --tb=short` — **59 PASS, 33 subtests PASS, 24 SKIP**, 31.62 s (final source). The immediately preceding revision passed 58/33 with 23 skips in 31.01 s.
- `git diff --check` — **PASS** on the scoped source/test delta.

New tests cover current-profile renewal, repeat idempotency, different-profile isolation, stale snapshot rejection **before insert** and after renewal, queued authorization preservation, rollback of both request and audit, and PostgreSQL lock deferral. The PostgreSQL concurrency test was added but was **SKIPPED locally** because no disposable PostgreSQL URL was configured. Hosted CI and exact PR merge-candidate verification remain pending at this source checkpoint.

No live resume, personal applicant data, external employer interaction, application submission, Cloudflare configuration or production service was touched. Source remains a partial MJ-REQ-264 improvement; automatic general employer-origin discovery and other provider breadth are not accepted by this slice.
