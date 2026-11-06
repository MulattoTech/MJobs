# MJ-TH-013 — explicit salary unit and disclosure policy

2026-11-04 UTC · Run `web-20261104T061507Z-5ad21717-0384-4992-8f1d-a39e828878e6` · Issue #918

## Implementation

- Jobs API takes `salary_period=YEAR|HOUR`, `salary_known_only` and `salary_unknown_allowed` with bounded non-negative USD min/max, mutually exclusive policy validation, and no implicit hourly/annual conversion.
- SQL applies compatible-currency/period, trustworthy numeric-endpoint, non-inverted-band, and non-estimate predicates **before LIMIT**. When a bound is present, an undisclosed salary is excluded by default and admitted only by explicit unknown-pay opt-in. An absent upper endpoint cannot establish a requested minimum, nor an absent lower endpoint establish a maximum. Known incompatible currencies/periods and malformed/estimated salaries never count as undisclosed.
- The web Jobs filter exposes salary unit and disclosure policy; state persists in the URL, through popstate, and in profile-scoped saved filters. Changing unit clears bounds. Legacy saved-filter serialization and default annual USD API behavior are retained.
- Job detail marks estimated pay explicitly and qualifies source-reported pay as not independently verified. Salary sort is still documented as annual USD; no sorting conversion is introduced.

## Executed evidence

- `python -m pytest -q -p no:cacheprovider tests/integration/test_job_sorting.py tests/integration/test_saved_search_filters.py --tb=short`: **38 passed, 35 passing subtests** (51.33 s).
- `npm --prefix apps/web run test -- salary-search-controls.test.tsx`: **5 passed**, one Vitest file.
- `python -m pytest -q -p no:cacheprovider tests/browser/test_saved_search_filters.py --tb=short` with isolated fixture API and freshly built SPA: **1 passed** (14.96 s), includes hourly USD query, unknown-pay opt-in, URL reload, saved-filter restoration and profile isolation.
- `vite build`: **PASS**, 140 modules transformed; existing large-bundle warning (986 kB JS, 266.9 kB gzip).
- `git diff --check`: **PASS**.
- `progress_dashboard --check` on primary checkout: CURRENT. Canonical acceptance remains unchanged until protected integration.

## Limitations / release gate

- Local `npm run typecheck` could not pass because the pre-existing shared node_modules lacks `@testing-library/dom`, `jsdom`, `@types/node`; the changed source emitted no diagnostic in that attempt.
- Local `npm run lint` could not execute because the same dependency tree lacks `eslint`. Neither result is represented as PASS. Hosted exact-head CI in its clean dependency install must verify both.
- Independent review, candidate-hosted CI, serialized protected integration, real production deployment and Cloudflare checks are **NOT RUN / PENDING**. No mutation of production, hosted infrastructure, canonical acceptance matrices, original requirement denominator, paid provider resources, or fleet emergency cutoff occurred.
- Overlap awareness: concurrent PRs for recently viewed jobs and resume provenance also touch `apps/web/src/main.tsx`. Integration owner must serialize and re-run checks against the actual combined merge candidate.
- Rollback: revert the feature commit through the protected PR workflow; no schema/data migration required.
