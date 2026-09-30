# MJobs Feature Completeness

Updated: 2026-09-30

## How to read this
This report intentionally separates the original MJobs product specification from later reference-project inspiration.
A percentage is a navigation aid, never a release claim. Only evidence-backed PASS counts as complete.

## Original specification
- Catalogue items: **366**
- PASS: **1** (0.27%)
- PARTIAL: **53** (14.48%)
- NOT_RUN: **312** (85.25%)
- Full-spec state: **PARTIAL**
- Release readiness: **not established**
The low PASS percentage does **not** mean only 0.27% of code exists. Many requirements are broad umbrella capabilities
and 53 are already PARTIAL. The current vertical slice is useful and tested, but the repository correctly refuses to
turn partial evidence into completed requirements.

## Current verified implementation spine
- Durable PostgreSQL/Redis worker architecture and migrations
- Local mock discovery -> company/ATS -> job -> match -> factual document -> application -> confirmation path
- Connected local UI and persistent tracker
- Public read-only Greenhouse discovery with provenance
- Factual PDF/DOCX generation and revisions
- Restart/outage recovery and duplicate-submission protection
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
