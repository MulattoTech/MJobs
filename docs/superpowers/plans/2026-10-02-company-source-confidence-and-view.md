# Company Source Confidence and View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make discovered company sources explainable and actionable through a trustworthy Companies/Sources view, while preserving user pin/watch choices and discovery fetch boundaries.

**Architecture:** Extend the existing company-source service with a read model that combines persisted sources, verified domains, ATS tenant evidence, discovery audit events, job-posting counts, and current-profile match counts. Add idempotent source pin and enable controls and show the model in the existing Sources tab. Do not claim unsupported scheduling or production discovery: absent next-check values are shown as “Not scheduled,” and fixture-only operations remain clearly restricted.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, SQLite/PostgreSQL, unittest; React 19, TypeScript, Vite, Vitest; existing Compose and Playwright acceptance.

**Spec:** `docs/spec/areas/company_ats_discovery.md` sections 78–81; GitHub issues #79–82 (MJ-REQ-078..081).

## Global Constraints

- External ATS/job content is untrusted; retain HTTP(S), origin, redirect, private-address, and port checks.
- Never present an unverified candidate domain as the official company domain.
- Preserve user-owned source priority and watch choices during automatic refresh.
- Do not imply production discovery, source verification, or next-check scheduling where no such path exists.
- Keep external application submission OFF and unrelated data unchanged.
- Record only evidence-backed requirement statuses; partial behavior stays PARTIAL.

## Review Focus

- A candidate domain that is not verified must never appear as an official domain; test read-model output with verified and unverified domain rows.
- A low-confidence source must emit `COMPANY_SOURCE_REVIEW_REQUIRED`; test just-below and at-threshold scores.
- Repeated pin, unpin, enable, or disable requests must be idempotent, and automatic refresh must retain a user pin; test service and API paths.
- Source counts must be isolated by `company_source_id` and current profile version; test sibling portals, duplicate postings, and absent profiles.
- Unsafe discovery URLs and cross-origin redirects must remain rejected; directly test private IPv4/IPv6, loopback, link-local, `file://`, unexpected ports, and redirect results without making external requests.

---

### Task 1: Company-source read model and confidence review state

**Files:**
- Modify: `services/company_discovery/sources.py`
- Modify: `tests/integration/test_company_sources.py`
- Modify: `tests/integration/test_company_discovery.py`

**Interfaces:**
- Produces `list_company_source_view(store, profile_id=None) -> list[dict]` without removing or changing `list_company_job_sources` behavior.
- Each result carries source/company identifiers and names; verified official domain (or `None`); career URL; provider/tenant/collection strategy; confidence; priority; enabled/origin; source-specific unique jobs found; current-profile relevant jobs; last checked; optional next check; and provenance evidence.
- A source with confidence `< 0.75` reports status `COMPANY_SOURCE_REVIEW_REQUIRED`; otherwise status reflects enabled/disabled without inventing a health state.
- `jobs_found` counts distinct canonical jobs whose postings use `company-feed:{source_id}`. `relevant_jobs` counts those with a match for the selected profile's current version; with no selected profile it is zero.
- `next_check` is `None` until a company-source schedule exists.
- Provenance is associated by explicit `company_source_id` in discovery/import audit payloads. Add that identifier where a discovery audit currently omits it; do not infer or fabricate missing historic evidence.

- [ ] **Step 1: Add failing service tests** for verified versus candidate domains, review threshold, evidence association, multiple portals, duplicate postings, profile-version match count, and no-profile counts.
- [ ] **Step 2: Run the new tests and confirm the missing read model/status behavior fails.**
- [ ] **Step 3: Implement the projection in `sources.py`** using existing persisted rows and audit events; keep SQL count queries bounded and source-specific.
- [ ] **Step 4: Add explicit source IDs to new discovery audit payloads** in `services/company_discovery/flywheel.py` and test both fixture and feed audit cases.
- [ ] **Step 5: Run `python -m unittest tests.integration.test_company_sources tests.integration.test_company_discovery -v` and require all tests to pass.**

### Task 2: Idempotent pin and enable controls

**Files:**
- Modify: `services/company_discovery/sources.py`
- Modify: `apps/api/main.py`
- Modify: `tests/integration/test_company_sources.py`

**Interfaces:**
- Add `set_company_source_pin(store, source_id, *, pinned: bool) -> dict`; pin sets priority to `USER_PINNED`, unpin returns priority to `NORMAL`, and both preserve watch origin.
- Automatic source refresh preserves `USER_PINNED` priority as well as existing `USER_WATCHED` priority.
- Add `set_company_source_enabled(store, source_id, *, enabled: bool) -> dict` and strict API inputs/routes for pin and enabled state. Missing source returns HTTP 404; malformed strict input returns 422.
- Record `COMPANY_SOURCE_PIN_CHANGED` and `COMPANY_SOURCE_ENABLED_CHANGED` audit actions only when a value changes.

- [ ] **Step 1: Add failing unit/API tests** for repeated state-setting, pin preservation across automatic upsert, independent watch and pin state, disabled source persistence, missing IDs, and strict booleans.
- [ ] **Step 2: Run those tests to confirm the new controls are absent or fail their assertions.**
- [ ] **Step 3: Implement service updates and audit events** with transactional read/update behavior.
- [ ] **Step 4: Expose `PUT /company-sources/{source_id}/pin` and `PUT /company-sources/{source_id}/enabled`.**
- [ ] **Step 5: Run `python -m unittest tests.integration.test_company_sources -v` and require all tests to pass.**

### Task 3: Expose company-source view and evidence through the API

**Files:**
- Modify: `apps/api/main.py`
- Modify: `services/company_discovery/sources.py`
- Modify: `tests/integration/test_company_sources.py`

**Interfaces:**
- Add `GET /company-sources/view?profile_id=...` returning the read-model list and its provenance.
- A supplied profile ID is validated through the existing profile read path; omitted profile yields zero relevant-job counts.
- Do not expose raw untrusted provider payloads as discovery instructions; provenance contains only the bounded audit fields used by the view.

- [ ] **Step 1: Add failing API tests** for response fields, profile validation, verified-domain handling, bounded evidence fields, and omitted profile behavior.
- [ ] **Step 2: Run the API tests and confirm they fail before route implementation.**
- [ ] **Step 3: Add the read route and serialize only the defined view contract.**
- [ ] **Step 4: Run `python -m unittest tests.integration.test_company_sources -v` and require all tests to pass.**

### Task 4: Companies/Sources view in the existing Sources tab

**Files:**
- Modify: `apps/web/src/main.tsx`
- Modify: `apps/web/src/style.css`
- Modify: `apps/web/src/api.test.ts` or add a focused source-view test file.
- Modify: `tests/e2e/` only if the existing browser test structure supports this view.

**Interfaces:**
- Add a typed company-source view model matching `GET /company-sources/view` and load it with the active profile.
- Display Company, Official Domain, Careers URL, ATS, Source Type, Confidence, Priority, Status, Jobs Found, Relevant Jobs, Last Checked, and Next Check. Show “Not scheduled” for absent next-check data and “Not verified” when official-domain evidence is absent.
- Provide working Pin/Unpin, Disable/Enable, existing Watch/Unwatch, and View Evidence actions with status/error feedback.
- Do not present Run Now, Rediscover, Verify, or Edit as working production actions while the production worker/source-edit path is absent. Show their unavailability explicitly, preserve the fixture-mode boundary for any supported discovery action, and keep MJ-REQ-081 PARTIAL until all required actions and scheduling are implemented.
- Keep the layout usable at existing mobile breakpoints and retain keyboard focus styles.

- [ ] **Step 1: Add a failing UI/API contract test** for rendering all required columns, explicit unavailable labels, current profile counts, and action wiring.
- [ ] **Step 2: Run the focused frontend test and confirm it fails before rendering is implemented.**
- [ ] **Step 3: Implement the view and supported controls** in the existing Sources tab; do not create a broad new app scaffold.
- [ ] **Step 4: Run `npm test --prefix apps/web` and `npm run build --prefix apps/web`; require both to pass.**
- [ ] **Step 5: Run the existing Playwright/Compose acceptance for Sources and confirm the Personal-mode discovery boundary remains intact.**

### Task 5: Direct fetch-policy regression coverage

**Files:**
- Modify: `tests/integration/test_company_discovery.py` or add `tests/unit/test_company_fetch_policy.py`.
- Modify: `services/company_discovery/fetch_policy.py` only if a tested gap is found.

**Interfaces:**
- Tests exercise the current non-fixture `_origin` URL decision with deterministic DNS stubs; they do not make external requests.
- Preserve fail-closed validation for scheme, hostname, credentials, redirects, private/loopback/link-local IPs, and unexpected ports.
- Report MJ-REQ-080 PARTIAL if the production discovery worker/client remains fixture-only, even when policy tests pass.

- [ ] **Step 1: Add tests for forbidden schemes, credentials, IPv4/IPv6 special ranges, private DNS results, mixed public/private DNS answers, allowed default ports, disallowed unusual ports, and cross-origin redirect rejection.**
- [ ] **Step 2: Run focused tests and confirm each protects an existing or missing boundary without real network access.**
- [ ] **Step 3: Change policy code only for demonstrated unsafe behavior and preserve fixture tests.**
- [ ] **Step 4: Run `python -m unittest tests.integration.test_company_discovery -v` and require all tests to pass.**

### Task 6: Full verification, evidence, and control-plane reconciliation

**Files:**
- Create: `docs/evidence/2026-10-02-company-source-confidence-and-view.json`
- Modify: `planning/CAPABILITY_MATRIX.json`
- Modify: `planning/IMPLEMENTATION_STATE.json`
- Modify: `planning/PR_QUEUE.json`
- Modify: `docs/FEATURE_COMPLETENESS.md`
- Modify: `apps/web` compatibility tests only if changed startup/API behavior requires it.

- [ ] **Step 1: Run focused company-source/discovery tests, the affected integration suite, then the full Python suite.**
- [ ] **Step 2: Run frontend tests/build, catalogue validation, and progress validation.**
- [ ] **Step 3: Run relevant isolated migration/Compose and browser acceptance; record external behavior not performed.**
- [ ] **Step 4: Write evidence with exact commands, UTC timestamp, targets, results/counts, evidence paths, known limitations, and requirement statuses.**
- [ ] **Step 5: Recalculate FEATURE_COMPLETENESS counts from CAPABILITY_MATRIX; update the PR queue target area to include the API and web files now in scope.**
- [ ] **Step 6: Reconcile GitHub again, commit the coherent change, push, open a draft PR, and verify workflows/reviews against its exact latest head before any merge.**

## Expected truthful requirement classifications

- MJ-REQ-078: PARTIAL unless the documented confidence examples and reliable low-confidence review workflow are all supported by evidence.
- MJ-REQ-079: PARTIAL unless every learned source has source-linked provenance, including historical sources that predate these changes.
- MJ-REQ-080: PARTIAL while production discovery lacks a non-fixture transport, even if local policy tests pass.
- MJ-REQ-081: PARTIAL while any required action or next-check scheduling is unavailable.

## Execution checkpoint 2026-10-02T10:45:17.027499+00:00
Implemented Tasks 1-5 with observed RED/GREEN coverage, a focused component, real browser controls and literal-safe display punctuation. Full verification: 235 Python tests, 6 frontend tests and production build, catalogue and progress validators PASS. No separate reviewer was dispatched; implementer diff/screenshot review performed. Task 6 local Compose/PostgreSQL proof is BLOCKED_ENVIRONMENT (engine unavailable); exact-head hosted CI remains required. All four umbrella requirements remain PARTIAL. Evidence: `docs/evidence/2026-10-02-company-source-confidence-and-view.json`.
