# MJobs Feature Completeness

Updated: 2026-09-30

## How to read this
This report intentionally separates the original MJobs product specification from later reference-project inspiration.
A percentage is a navigation aid, never a release claim. Only evidence-backed PASS counts as complete.

## Original specification
- Catalogue items: **366**
- PASS: **3** (0.82%)
- PARTIAL: **55** (15.03%)
- NOT_RUN: **308** (84.15%)
- Full-spec state: **PARTIAL**
- Release readiness: **not established**
The low PASS percentage does **not** mean only 0.55% of code exists. Many requirements are broad umbrella capabilities
and 55 are already PARTIAL. The current vertical slice is useful and tested, but the repository correctly refuses to
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
- A first-class source adapter interface and provider-independent collection path are PASS for MJ-REQ-016. Greenhouse remains the only production adapter. A synthetic two-company batch verifies distinct employers can reuse a requisition without overwriting posting provenance. The documented API-first collection strategy is PARTIAL for MJ-REQ-018 pending broader production source coverage (`docs/evidence/2026-09-30-source-adapter-foundation.json`).
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
