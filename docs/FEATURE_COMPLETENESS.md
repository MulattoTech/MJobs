# MJobs Feature Completeness

Updated: 2026-10-01

## How to read this
This report intentionally separates the original MJobs product specification from later reference-project inspiration.
A percentage is a navigation aid, never a release claim. Only evidence-backed PASS counts as complete.

## Original specification
- Catalogue items: **366**
- PASS: **24** (6.56%)
- PARTIAL: **60** (16.39%)
- NOT_RUN: **282** (77.05%)
- Full-spec state: **PARTIAL**
- Release readiness: **not established**
The low PASS percentage does **not** mean only 6.56% of code exists. Many requirements are broad umbrella capabilities
and 60 are already PARTIAL. The current vertical slice is useful and tested, but the repository correctly refuses to
turn partial evidence into completed requirements.

## Current verified implementation spine
- Durable PostgreSQL/Redis worker architecture and migrations
- Local mock discovery -> company/ATS -> job -> match -> factual document -> application -> confirmation path
- Connected local UI and persistent tracker
- Public read-only Greenhouse discovery with provenance
- Factual PDF/DOCX generation and revisions
- Restart/outage recovery and duplicate-submission protection
- Evidence-linked, atomic implementation milestone journal with reload, failed-write preservation, and remote CI evidence (MJ-REQ-008 PARTIAL; `docs/evidence/2026-09-30-ci-428.json`)
- Separate personal operation mode blocks fictional actions at the API and worker; fixture verification remains available. Direct Compose defaults to PERSONAL without the fictional mock service. Both exact-head CI runs passed actual fixture mock and personal no-mock service checks, isolated personal Chromium, and live public read/browser proof of 21 jobs; an earlier optional provider read timed out. MJ-REQ-009 remains PARTIAL (`docs/evidence/2026-09-30-ci-428-compose-isolation.json`).
- A database identity slice binds each empty database to FIXTURE or PERSONAL and refuses startup or worker claims against a conflicting or nonempty unbound database. Legacy data is preserved for explicit snapshot-matched owner adoption. Local 91-test and temporary SQLite migration checks passed; both exact-head CI runs passed explicit PostgreSQL revision and stored-mode assertions, fixture recovery and separate personal Chromium/public-read proof (`docs/evidence/2026-09-30-ci-428-workspace-identity.json`). MJ-REQ-009 remains PARTIAL.
- The preferred technology stack is PASS for MJ-REQ-011 after both deployed CI runs exercised the pinned Python/FastAPI/SQLAlchemy/Alembic, TypeScript React/Vite, PostgreSQL, Redis/RQ, Playwright Chromium and Compose paths. The broader product architecture MJ-REQ-010 is PARTIAL pending global/company discovery, fuller resolution/deduplication, AI and supported real ATS services (`docs/evidence/2026-09-30-architecture-stack-reconciliation.json`).
- A first-class source adapter interface and provider-independent collection path are PASS for MJ-REQ-016. Greenhouse remains the only production adapter. A synthetic two-company batch verifies distinct employers can reuse a requisition without overwriting posting provenance. The documented API-first collection strategy is PARTIAL for MJ-REQ-018 pending broader production source coverage. Both exact-head CI runs deployed the source worker and personal browser, read 21 public Greenhouse jobs and made zero external application requests (`docs/evidence/2026-09-30-ci-429.json`).
- Three independent RQ consumers for application browser, document, and discovery work, plus matching Compose configuration and live readiness, establish MJ-REQ-012 PASS. A Docker Desktop relaunch exposed missing PostgreSQL/Redis restart policies; the existing local stack was repaired in place and a fresh isolated PERSONAL deployment passed first-run, Chromium, restart-policy and stored-profile relaunch checks (`docs/evidence/2026-09-30-pr004-local-restart.json`). Both earlier implementation-head CI runs passed fixture/personal Compose, Python/frontend, Chromium, recovery and public source checks, with twelve retained artifacts (`docs/evidence/2026-09-30-ci-430.json`).
- Offline PostgreSQL and document backup with SHA-256 manifest verification and fresh-project restore passed local FIXTURE and PERSONAL Docker exercises. A queued fixture operation recovered with fresh Redis; a personal prepared real-job tracker entry and public posting provenance survived. Restores refused occupied targets, checked exact document inventory, reached readiness and kept external submission disabled (`docs/evidence/2026-09-30-pr004-backup-restore-local.json`).
- Fresh direct PERSONAL Compose startup migrated PostgreSQL and passed connected Chromium upload of a synthetic PDF. Profile-scoped PDF/DOCX/UTF-8 TXT originals are stored unreviewed, bounded and checksum verified; uploading does not approve CareerFacts. A fresh-project restore retained the original bytes and metadata, and a pre-upload PERSONAL archive migrated without losing one profile or 21 jobs. MJ-REQ-013/014/090/167/168/173 remain PARTIAL: native extraction/review, protected host configuration, physical host restart and complete acceptance are open (`docs/evidence/2026-09-30-pr004-upload-restore-local.json`).
- Typed source capabilities/policy, persistent scheduled search profiles, and a database-only scheduler are PASS for MJ-REQ-019..021; MJ-REQ-022 remains PARTIAL for broader search-profile lifecycle coverage. A fresh isolated Compose deployment, previous-schema PostgreSQL upgrade, headless UI flow and three durable public Greenhouse scheduler operations passed; 21 jobs remained healthy and no external application request occurred (docs/evidence/2026-10-01-source-policy-scheduler.json).
- Deterministic structured query semantics, location/work-arrangement filtering, remote restriction classification and employment-type normalization are PASS for MJ-REQ-023..026. Search profiles persist include/exclude title, required/optional/excluded keyword, exact phrase, technology alias, company, location, remote-region and coordinate-backed radius criteria; radius filtering refuses to invent geocodes. Focused, affected and 120-test full Python suites passed with catalogue/progress validation (docs/evidence/2026-10-01-search-semantics.json).
- Experience-level normalization, salary parse/filter semantics and date filtering are PASS for MJ-REQ-027..030. Original seniority phrases are preserved, compensation provenance stays distinct, unknown periods/dates remain unknown, and canonical jobs persist true provider-reported posted times separately from MJobs first/last-seen timestamps. The focused 29-test slice and 126-test full Python suite passed (docs/evidence/2026-10-01-experience-salary-date.json).
- Normalized skill filtering/taxonomy, conservative title normalization and education-requirement parsing are PASS for MJ-REQ-031..034. Alias matches retain original wording, no technology is globally required, short aliases avoid common-word false positives, title families avoid unrelated administrator over-collapse, and required/preferred/equivalent education language remains distinct. The focused 41-test slice and 133-test full Python suite passed (docs/evidence/2026-10-01-skill-title-education.json).
- Experience-year requirements, certification strength, clearance level and work-authorization statements are PASS for MJ-REQ-035..038. Parsers preserve matched wording and local clauses; search profiles can explicitly exclude clearance/work-authorization requirements without inferring the user's own status. The focused 40-test slice and 145-test full Python suite passed (docs/evidence/2026-10-01-requirement-parsing.json).
- Explicit user negative filters are PASS for MJ-REQ-039. Jobs retain evidence for on-call, night shift, 1099, commission, door-to-door, clearance, mandatory relocation and stated travel percentage; profiles exclude only conditions the user selects, and unknown travel is never guessed. The focused 45-test slice and 150-test full Python suite passed (docs/evidence/2026-10-01-negative-filters.json).
- Real external submission remains disabled
- GitHub Actions passed the final #367-#370 PR heads and the connected UI, mock application, and restart recovery workflow; see `docs/evidence/2026-09-30-ci-370.json`
## Reference-project gap catalogue
Canonical issues: **MJ-REF-001 through MJ-REF-042**.
These are additive ideas discovered by comparing JobSpy, CareerWeaver, Resume Matcher, AutoApply,
Personal JobPilot, ResuMorph and AIHawk.

Reference gaps must be reported separately from the original 366 requirements:
- PASS: 0
- PARTIAL: 0
- NOT_RUN: 42

When implementation overlaps existing MJobs behavior, the reference issue still requires its own acceptance evidence
before it becomes PASS. Closing a reference issue must never auto-close an original MJ-REQ issue or vice versa.
## Completion surfaces that should appear in the repository/UI
1. **Original Spec** — PASS / PARTIAL / NOT_RUN / blockers across 366 MJ-REQ items.
2. **Reference Gaps** — PASS / PARTIAL / NOT_RUN across 42 MJ-REF items.
3. **Current Milestone** — evidence-backed vertical slice currently usable.
4. **Release Readiness** — independent acceptance gate, never inferred from a percentage.
5. **External/Environment Blockers** — visible separately so a blocked integration cannot masquerade as product failure.

## Update rule
Whenever a PR changes requirement evidence, update the relevant matrix and this summary from machine-readable state.
Do not hand-edit percentages without reconciling evidence.
