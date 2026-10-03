# Reference Gap PR Queue

This queue complements `planning/PR_QUEUE.json`; it does not replace original dependency order.

## Gate 0 — integrated stacked spine
Completed 2026-09-30: #367 -> #368 -> #369 -> #370 merged in dependency order. The workflow-scope blocker was resolved, and the final #370 head passed GitHub Actions including the connected UI and recovery journey (`docs/evidence/2026-09-30-ci-370.json`). The primary `planning/PR_QUEUE.json` remains next in line.

## RG-PR-001 — Career memory and presentation
Issues: #371, #372, #373, #375, #380, #384.
Depends on: mature CareerFact/import/version foundations.

## RG-PR-002 — Application preflight and agent contracts
Issues: #374, #383, #403, #416.
Depends on: stable application/form contracts.

## RG-PR-003 — Document quality workbench
Issues: #376, #377, #424.
Depends on: document versioning and factual validation.

## RG-PR-004 — AI budgets and telemetry
Issues: #381, #385.
Depends on: provider abstraction.

## RG-PR-005 — Quality and provenance gates
Issues: #382, #400, #407.
Depends on: runnable local verification.

## RG-PR-006 — Durable failure and artifact lifecycle
Issues: #386, #387.
Depends on: durable task and artifact ownership.

## RG-PR-007 — Browser companion and bounded control
Issues: #388, #389, #402.
Depends on: stable local API and semantic form mapper.

## RG-PR-008 — Application reconciliation and advisor UX
Issues: #390, #391, #393, #394.
Depends on: application tracker and company model.

## RG-PR-009 — Career follow-through
Issues: #392, #395, #396.
Depends on: stable application/career data.

## RG-PR-010 — Desktop operations and agent support
Issues: #397, #398, #399, #426.
Depends on: deployment and settings stability.

## RG-PR-011 — Discovery breadth and provenance
Issues: #401, #404, #405, #406.
Depends on: canonical source/job model.

## RG-PR-012 — Role scoring and portfolio comparison
Issues: #408, #415.
Depends on: matching engine and profile configuration.

## RG-PR-013 — Interview and language enrichment
Issues: #378, #379.
Depends on: verified facts/documents and stable UI strings.

## Scheduling rule
Open a draft only after real implementation exists. Keep active PR count within repository policy. Prefer merging prerequisites before dependents. Reference PRs may remain queued while higher-priority original requirements are dependency-eligible.

## Approved commercial additions (2026-10-03)

| ID | Issue | Feature | Priority | Owner | Acceptance prerequisites |
|---|---|---|---|---|---|
| MJ-REF-043 | #454 | LinkedIn profile import into CareerFact staging | P3 | career_profile_documents | MJ-REQ-092, MJ-REQ-094, MJ-REQ-096 |
| MJ-REF-044 | #455 | LinkedIn profile optimizer with section-level recommendations | P4 | career_profile_documents | MJ-REQ-092, MJ-REQ-095, MJ-REQ-096 |
| MJ-REF-045 | #456 | Networking/contact CRM linked to companies jobs and applications | P4 | web_api_analytics | MJ-REQ-054, MJ-REQ-121, MJ-REQ-170, MJ-REQ-239 |
| MJ-REF-046 | #457 | Recruiter and hiring-manager discovery workflow | P4 | company_ats_discovery | MJ-REQ-054, MJ-REQ-062, MJ-REQ-080, MJ-REF-045 |
| MJ-REF-047 | #458 | Per-job tasks reminders and due dates | P4 | web_api_analytics | MJ-REQ-121, MJ-REQ-153, MJ-REQ-154, MJ-REQ-204, MJ-REQ-268 |
| MJ-REF-048 | #459 | First-class job-search Campaign or Loop object | P4 | orchestration_data_security | MJ-REQ-021, MJ-REQ-022, MJ-REQ-124, MJ-REQ-149, MJ-REQ-150, MJ-REQ-151, MJ-REQ-155, MJ-REQ-156, MJ-REQ-276 |
| MJ-REF-049 | #460 | Campaign performance analytics | P5 | web_api_analytics | MJ-REQ-201, MJ-REQ-202, MJ-REQ-268, MJ-REF-048 |
| MJ-REF-050 | #461 | Daily matched-jobs review inbox and digest | P4 | web_api_analytics | MJ-REQ-112, MJ-REQ-118, MJ-REQ-186, MJ-REQ-196, MJ-REQ-204 |
| MJ-REF-051 | #462 | ATS-specific resume compatibility profiles and scoring | P4 | career_profile_documents | MJ-REQ-063, MJ-REQ-097, MJ-REQ-112 |
| MJ-REF-052 | #463 | Resume impact and metrics analyzer | P4 | career_profile_documents | MJ-REQ-092, MJ-REQ-095, MJ-REQ-096 |
| MJ-REF-053 | #464 | Job-description summarizer and decision brief | P4 | matching_ai | MJ-REQ-040, MJ-REQ-087, MJ-REQ-112, MJ-REQ-118 |
| MJ-REF-054 | #465 | One-click reviewable Application Kit | P4 | application_automation | MJ-REQ-094, MJ-REQ-095, MJ-REQ-096, MJ-REQ-100, MJ-REQ-121, MJ-REQ-136, MJ-REQ-137, MJ-REQ-197, MJ-REF-024 |
| MJ-REF-055 | #466 | Resume-version outcome analytics | P5 | web_api_analytics | MJ-REQ-094, MJ-REQ-121, MJ-REQ-201, MJ-REQ-268 |
| MJ-REF-056 | #467 | Job freshness and urgency ranking | P3 | core_search_model | MJ-REQ-032, MJ-REQ-083, MJ-REQ-085, MJ-REQ-086, MJ-REQ-190 |
| MJ-REF-057 | #468 | In-page resume and cover-letter tailoring from browser companion | P4 | career_profile_documents | MJ-REQ-095, MJ-REQ-096, MJ-REQ-100, MJ-REQ-170, MJ-REQ-205, MJ-REF-018, MJ-REF-019 |
| MJ-REF-058 | #469 | Recorded mock-interview sessions | P5 | operations_ux_ci | MJ-REQ-167, MJ-REQ-168, MJ-REQ-170, MJ-REF-008, MJ-REF-042 |
| MJ-REF-059 | #470 | Interview delivery and content feedback history | P5 | operations_ux_ci | MJ-REQ-092, MJ-REQ-170, MJ-REF-058 |

The machine-readable source of scope, overlaps, prerequisites, queue foundations, acceptance and tests is `REFERENCE_GAP_MATRIX.json.commercial_features`. These 17 entries are NOT_RUN. A prerequisite must have evidence-backed PASS; merging an incremental foundation PR is insufficient. Original safety, identity, durability and product work retain priority.

Coherent future delivery groups (no branches or PRs created):

- Profile intelligence: 043, 044, 052, after verified career facts and review.
- Contacts and follow-through: 045, then 046; 047 after durable application state and notifications.
- Campaigns and review: 048, then 049; 050 after matching/review foundations.
- Evidence-based materials: 051, 053, 054, with their individual gates.
- Outcome evidence: 055; freshness ranking: 056 after lifecycle/source-quality foundations.
- Browser tailoring: 057 after REF-018/019 and secure local document APIs.
- Interview practice: 058 after REF-008/042 and private storage, then 059.
