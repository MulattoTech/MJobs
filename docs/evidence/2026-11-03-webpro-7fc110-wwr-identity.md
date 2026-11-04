# WWR native-identity prefilter integrity — 2026-11-03

- Run: `webpro-20261103T073456Z-7fc110`
- Claim: `webpro-claims/WWR-SOURCE-DUPLICATE-JOB-ID` (owner commit `c5c298081dbc2d8c32d0b245be778c668cbc02f7`)
- Base main: `63cb1042e08375f7bda1f9dad378209cca856167`
- Requirement: `MJ-REQ-017` (remains PARTIAL)

## Problem and baseline

The WWR RSS adapter parsed records and immediately applied client-side category, text, and result-limit filters. A caller could therefore receive a successful subset even if the original feed had conflicting records with the same native posting identity. Exact duplicate rows also leaked through the direct search() result, although downstream collection already had an identity collision check.

Red regressions were executed against the unmodified adapter in this isolated worktree: one failing test and eight failing parameterized subtests. The failures proved both a duplicate direct-search result and that opposing conflict orderings were accepted under unfiltered, limit=1, category and text filters.

## Change

- Check the full parsed RSS feed for collisions using the same native posting ID extraction as normalize().
- Preserve first-seen ordering; coalesce only structurally identical raw duplicates.
- Reject any two differing raw records for one native identity, independent of order, before applying local filters or limits; set adapter health to SOURCE_DUPLICATE_JOB_ID.
- Poll the existing cancellation callback during validation. No extra network request, retry, browser interaction, identity rewrite or raw feed logging is introduced.
- Keep the existing collector-level collision/rollback guard in place.

## Executed verification

```text
python -m pytest -q -p no:cacheprovider tests/integration/test_wwr.py tests/integration/test_source_value_integrity.py tests/unit/test_application_links.py --tb=short
46 passed, 1 skipped, 22 subtests passed in 18.65s
```

The skipped check is the opt-in live WWR network test; no real provider access was requested. Other broad repository, browser, hosted CI and production tests are NOT_RUN at this source checkpoint and require separate PR/candidate evidence.

## Boundaries and acceptance

The observed upstream WWR conflicting records are not repaired or assigned an invented winner. A conflicting full feed still fails closed; no partial accepted inventory, altered stored jobs, provider contact, employer application or production deployment is claimed. Source `MJ-REQ-017` stays PARTIAL, and P0/P1 PASS totals do not change from this correction alone.
