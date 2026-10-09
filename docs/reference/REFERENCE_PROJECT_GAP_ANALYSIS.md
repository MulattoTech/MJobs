# MJobs Reference-Project Gap Analysis
## Audit date: 2026-09-30

This document compares MJobs against seven public reference projects and turns useful differences into traceable work.
It is an inspiration and gap catalogue, not permission to copy incompatible code or weaken MJobs safety policy.

## Reference set
1. JobSpy — https://github.com/speedyapply/JobSpy
2. CareerWeaver — https://github.com/idea-torx/CareerWeaver
3. Resume Matcher — https://github.com/srbhr/Resume-Matcher
4. AutoApply — https://github.com/Liam-Frost/AutoApply
5. Personal JobPilot — https://github.com/TheCromazone/Personal-JobPilot
6. ResuMorph — https://github.com/hannibalevit/resumorph
7. AIHawk — https://github.com/feder-cr/aihawk_mcp_server

## MJobs baseline at audit
- Current working implementation is the stacked chain #367 -> #368 -> #369 -> #370.
- Capability matrix: 366 catalogue items; 1 PASS, 53 PARTIAL, 312 NOT_RUN at audit time.
- Verified local slice already includes durable PostgreSQL/Redis workers, mock E2E, a connected UI, factual PDF/DOCX output,
  public read-only Greenhouse discovery, provenance, persistence/restart recovery, and controlled mock submission.
- Real external submission remains disabled and the full product specification remains incomplete.
- Remote GitHub Actions execution is BLOCKED_EXTERNAL by the current workflow OAuth scope.

Post-audit reconciliation (2026-09-30): the workflow authorization was restored;
#367 through #370 were verified and merged in order. The final connected UI
Actions evidence is `docs/evidence/2026-09-30-ci-370.json`. The baseline above
records the audit-time state and is not the current release decision.

## Project nature and spirit

### JobSpy — broad normalized discovery library
Spirit: make many job boards look like one simple Python API and return a normalized tabular result quickly.
Strengths to borrow: adapter breadth, concurrent collection, normalized source contracts, predictable caller-facing data.
Do not borrow: proxy/block-circumvention as a product goal. MJobs must retain explicit source policy and no-evasion rules.

### CareerWeaver — truth-preserving career compiler
Spirit: one canonical verified fact graph can be rendered differently for different role personas without inventing facts.
Strengths to borrow: persona/lens separation, unverified-claim quarantine, deterministic no-key degradation, agent-first JSON CLI,
human-held application completion, and local-first ownership.
MJobs already shares its strongest invariant: generated material must be grounded in verified facts.

### Resume Matcher — interactive document intelligence workbench
Spirit: make resume/JD alignment visible and editable, not a hidden one-shot generation call.
Strengths to borrow: interview preparation, keyword highlighting, layout/template choice, editable/reorderable sections,
multi-language UI/content, outreach generation, and rich comparison UX.

### AutoApply — production-style local operator platform
Spirit: treat personal job automation like an observable durable service, with queues, retries, artifacts, operator controls and cost awareness.
Strengths to borrow: story bank + bullet pool, curated document promotion, recurring automation plans, dead-letter queue,
stuck/failed task UX, quarantine/restore/purge cleanup, atomic file writes, provider/model catalogues, rate-limit gates and cost telemetry.
Its PolyForm Noncommercial license means MJobs should reimplement ideas independently rather than copy restricted code.

### Personal JobPilot — opinionated autonomous personal pipeline
Spirit: optimize the whole search loop for one owner, run continuously, keep it cheap/local, and expose every decision.
Strengths to borrow: configurable role archetype rubrics, deterministic weighted final scoring, JD quality-audit/regeneration loop,
interview-prep packs, networking outreach drafts, watchdog/autostart ergonomics, local-first LLM defaults, fast live dashboard,
and a browser extension that assists the user without clicking Submit.

### ResuMorph — privacy-first browser-side tailoring companion
Spirit: bring useful assistance to the page the user is already viewing while keeping data and keys under user control.
Strengths to borrow: scan-current-page workflow, browser extension surface, inline non-sensitive answer drafting with preview,
local history, output-format preferences, explicit privacy/security documentation, and checked-in agent-development instructions.

### AIHawk — external-agent browser control surface
Spirit: expose a real browser as a reusable tool surface that coding agents can control, with persistent profiles and live visibility.
Safe ideas to borrow: MCP-style browser control, session persistence, key isolation, local session/screenshot ownership, live browser view.
Do not adopt stealth fingerprinting, proxy identity manipulation, anti-bot evasion, or any mechanism whose purpose is avoiding site controls.

## Feature-by-feature comparison matrix

| Capability | Reference inspiration | MJobs spec/current state | Action |
|---|---|---|---|
| Multi-board normalized discovery | JobSpy | SPECIFIED; mostly NOT_RUN beyond Greenhouse | Existing MJ-REQ-016/017 children |
| Concurrent source collection | JobSpy | SPECIFIED; PARTIAL bounded workers | Existing MJ-REQ-005/020 |
| LinkedIn/Indeed/ZipRecruiter/Glassdoor/Google breadth | JobSpy | SPECIFIED provider children; NOT_RUN | Existing provider issues |
| Proxy/block bypass | JobSpy | Explicitly incompatible | REJECT |
| Canonical verified career fact graph | CareerWeaver | SPECIFIED; PARTIAL | Existing MJ-REQ-092 |
| Unverified-claim blocking | CareerWeaver | SPECIFIED; PARTIAL | Existing MJ-REQ-096 |
| Persona/lens rendering of same facts | CareerWeaver | NOT explicitly specified | REF-GAP-001 |
| Deterministic operation without LLM | CareerWeaver | Architectural principle, partial | Map MJ-REQ-108/271 |
| Machine-readable agent-first CLI | CareerWeaver | CLI exists in spec, JSON contract absent | REF-GAP-008 |
| Human-held submit boundary | CareerWeaver | SPECIFIED and implemented for current slice | Existing MJ-REQ-123/124 |
| Resume/JD match score | Resume Matcher | SPECIFIED; PARTIAL | Existing MJ-REQ-112..118 |
| Keyword highlighting/explanation | Resume Matcher | Explainability specified; visual highlighting gap | Map REF-GAP-006 UX |
| Resume template/layout selection | Resume Matcher | ATS-friendly output specified; user template control absent | REF-GAP-006 |
| Drag/reorder resume sections | Resume Matcher | Not explicit | REF-GAP-006 |
| Interview preparation pack | Resume Matcher / JobPilot | NOT specified | REF-GAP-003 |
| Networking/recruiter outreach drafts | Resume Matcher / JobPilot | NOT specified | REF-GAP-004 |
| Multi-language UI/content | Resume Matcher | NOT specified | REF-GAP-005 |
| Multi-JD comparison/optimization | Resume Matcher roadmap | NOT specified | REF-GAP-007 |
| Story bank / STAR examples | AutoApply | Answer bank exists; story memory absent | REF-GAP-002 |
| Tagged bullet pool | AutoApply | Career facts exist; reusable bullet pool absent | REF-GAP-002 |
| Curated/promoted trusted document bases | AutoApply | Document library specified; promotion workflow absent | REF-GAP-002 |
| User-defined recurring automation plans | AutoApply | Scheduler/rules specified; NOT_RUN | Existing MJ-REQ-149/155/275 |
| Dead-letter queue with retry/discard UX | AutoApply | Retry/error UI generic; DLQ semantics absent | REF-GAP-009 |
| Artifact cleanup quarantine/restore/purge | AutoApply | Retention specified; quarantine recovery absent | REF-GAP-010 |
| Atomic generated-artifact writes | AutoApply | Durability implied, not explicit | REF-GAP-010 |
| Provider/model catalogue | AutoApply | Provider abstraction specified; catalogue UX absent | REF-GAP-011 |
| Per-provider rate-limit gate | AutoApply | concurrency/cost controls specified | Map MJ-REQ-005/110 |
| AI cost telemetry | AutoApply | cost controls specified; telemetry UX absent | REF-GAP-011 |
| Resilient launcher/check mode/port negotiation | AutoApply | local startup exists; portability ergonomics gap | REF-GAP-017 |
| Configurable role archetypes/rubrics | JobPilot | generic weights specified; reusable archetype templates absent | REF-GAP-015 |
| Deterministic weighted final score after AI dimensions | JobPilot | deterministic score specified | Existing MJ-REQ-112/117 |
| JD quality audit and one-pass regeneration feedback | JobPilot | factual validation exists; quality feedback loop absent | REF-GAP-016 |
| Local Ollama-first mode | JobPilot / CareerWeaver | provider abstraction includes local path; NOT_RUN | Existing MJ-REQ-108 |
| Continuous scheduler/watchdog | JobPilot | scheduler/recovery specified; process supervisor UX gap | REF-GAP-017 |
| SSE/live activity feed | JobPilot | SPECIFIED; PARTIAL | Existing MJ-REQ-206 |
| Kanban application pipeline | JobPilot | SPECIFIED; PARTIAL | Existing MJ-REQ-195 |
| Browser autofill extension | JobPilot / ResuMorph | Future browser extension named but not defined | REF-GAP-012 linked MJ-REQ-259 |
| Extension offline cached-profile fallback | JobPilot | NOT specified | REF-GAP-012 |
| Scan current job page from browser | ResuMorph | NOT specified | REF-GAP-012 |
| Inline non-sensitive answer drafting/preview | ResuMorph | Answer bank/form engine exists; inline UX absent | REF-GAP-013 |
| Local artifact/history review | ResuMorph | SPECIFIED; PARTIAL | Existing MJ-REQ-093/238 |
| Explicit privacy/data-flow documentation | ResuMorph | Security specified; user-facing transparency gap | REF-GAP-018 |
| MCP/browser-agent control surface | AIHawk | NOT specified | REF-GAP-014 |
| Persistent reusable browser profile | AIHawk | SPECIFIED; NOT_RUN | Existing MJ-REQ-126 |
| Live browser visibility/session inspection | AIHawk | Diagnostics generic; operator live-view absent | REF-GAP-014 |
| Secret/key isolation from browser process | AIHawk | Secret management specified; verification test can improve | Map MJ-REQ-175/225 |
| Stealth/fingerprint/proxy evasion | AIHawk | Explicitly prohibited | REJECT |

## True reference gaps to add
The following are intentionally separate from the 366 original requirements. They are additive product/governance ideas,
not replacements for original requirements, and must not jump dependency order ahead of P0-P3 core correctness.

1. REF-GAP-001 Persona/lens-based career presentation using the same verified fact graph.
2. REF-GAP-002 Story bank, tagged bullet pool, and promotion of trusted generated materials.
3. REF-GAP-003 Role-specific interview preparation packs grounded in verified facts and the target JD.
4. REF-GAP-004 Networking/recruiter/hiring-manager outreach drafts that never send automatically or invent contacts.
5. REF-GAP-005 Internationalized UI plus explicit output-language control.
6. REF-GAP-006 ATS-safe resume layout/template customization, section ordering, and keyword-highlight comparison UX.
7. REF-GAP-007 Compare/optimize one career profile against multiple job descriptions.
8. REF-GAP-008 Stable machine-readable CLI/API contract for agent orchestration.
9. REF-GAP-009 Dead-letter task state, stuck/failed operator view, retry/discard with audit trail.
10. REF-GAP-010 Atomic artifact writes plus audited quarantine/restore/purge retention workflow.
11. REF-GAP-011 AI provider/model catalogue, per-provider limits, usage/cost telemetry and model selection UX.
12. REF-GAP-012 Browser extension: scan current page, assisted autofill, cached safe offline profile, never auto-submit.
13. REF-GAP-013 Inline non-sensitive answer drafting with preview/edit/copy/insert and mandatory review rules.
14. REF-GAP-014 Optional MCP-compatible browser/application control surface and live session view, with no stealth/evasion.
15. REF-GAP-015 Reusable configurable role-archetype/rubric templates without hard-coding occupations.
16. REF-GAP-016 Post-generation JD quality audit and bounded feedback/regeneration loop.
17. REF-GAP-017 Resilient local supervisor/launcher: check mode, port negotiation, restart health, optional login autostart.
18. REF-GAP-018 User-facing privacy/security/data-flow documentation and diagnostics.

## Dependency policy for reference gaps
- First finish/integrate the current verified spine and resolve the CI publication/execution gate.
- P0/P1/P2/P3 original requirements always outrank convenience/reference features.
- REF-GAP-002, 003, 004, 006, 007, 013, 015 and 016 depend on a mature verified CareerFact/document/matching foundation.
- REF-GAP-009/010 depend on durable operation/artifact ownership semantics.
- REF-GAP-012/014 depend on stable application/form/browser APIs and must preserve no-auto-submit defaults.
- REF-GAP-005 and 018 can proceed independently once core UI/config schemas stabilize.
- REF-GAP-017 should accompany deployment/operations hardening rather than become a separate platform.

## Completion display rule
MJobs must distinguish:
- ORIGINAL SPEC COMPLETION: evidence state across the 366 original catalogue items.
- REFERENCE GAP COMPLETION: evidence state across REF-GAP items.
- RELEASE READINESS: independent gate; never inferred from percentages.
No percentage may convert PARTIAL or NOT_RUN into PASS. A visual percentage can count only evidence-backed PASS as complete,
with PARTIAL shown separately.

## Source and licensing note
This audit is based on public project documentation/code structure observed on 2026-09-30.
Reference projects have different licenses. Use behavior and architecture as inspiration, then implement independently.
Never vendor or copy code without a compatible license review and attribution decision.

For current pinned upstream commits, commit dates, license-at-ref blob IDs, relicense history,
and the enforced clean-room/no-evasion review, see
[`REFERENCE_PROJECTS.json`](REFERENCE_PROJECTS.json) and
[`REFERENCE_IMPLEMENTATION_POLICY.md`](REFERENCE_IMPLEMENTATION_POLICY.md). The dated registry
supersedes this section only for current provenance and licensing metadata; the 2026-09-30
capability observations above remain a historical snapshot.


## Canonical GitHub reference-gap backlog

The canonical namespace is `MJ-REF`. Concurrent audit work produced a richer catalogue; duplicate `REF-GAP` issues were closed rather than allowing two sources of truth.

### Career facts, documents, and matching
- #371 MJ-REF-001 — Role lenses and archetype-specific presentation strategies
- #372 MJ-REF-002 — Career story bank and reusable bullet pool — PARTIAL on local main: approved, current, fact-linked story revisions can be explicitly reviewed for answers and cover letters; answer export history and generated cover-letter history retain provenance-only story revision/fact links. Cover-letter story wording stays exact and review-required. Resume story selection and broader story-bank acceptance remain incomplete. Evidence: `docs/evidence/2026-10-09-career-story-answer-citations.json`, `docs/evidence/2026-10-09-career-story-export-citations.json`, `docs/evidence/2026-10-09-cover-letter-story-reuse.json`.
- #373 MJ-REF-003 — Enforced global avoid-list for generated career content
- #375 MJ-REF-005 — Deterministic no-LLM fallback for core career workflows
- #376 MJ-REF-006 — Deterministic resume page-fit and layout measurement — PASS on local main after measured-success connected browser acceptance; deterministic layout measurement, protected-fact preservation, bounded attempts, explicit preview review, and durable generated revision readback are verified. Evidence: `docs/evidence/2026-10-09-resume-page-fit-acceptance-audit.json`.
- #377 MJ-REF-007 — Resume workspace with templates, section ordering, and keyword highlighting
- #378 MJ-REF-008 — Grounded interview-preparation workspace
- #379 MJ-REF-009 — Multilingual UI and generated-material support
- #380 MJ-REF-010 — Deterministic resume preservation and claim-level mutation guard
- #384 MJ-REF-014 — Least-data profile lookup interface for AI tasks
- #395 MJ-REF-025 — Grounded networking outreach drafting
- #396 MJ-REF-026 — Skill-gap heatmap and evidence-driven upskilling plan — PARTIAL on local main: the deterministic heatmap now shows gap-specific counts for user-reported applications and interviews and prioritizes interview gaps, application gaps, required-gap frequency, and total gaps in that order. AI-guided theme grouping or course recommendations remain unimplemented. Evidence: `docs/evidence/2026-10-09-skill-gap-priority.json`.
- #408 MJ-REF-038 — Role-specific scoring profiles and configurable specialized fit signals
- #415 MJ-REF-039 — Multi-job-description comparison and optimization workspace
- #424 MJ-REF-041 — Post-generation JD quality audit and bounded regeneration loop

### Application/browser assistance and safety
- #374 MJ-REF-004 — ATS question preflight before browser launch
- #383 MJ-REF-013 — Bounded agent ToolRegistry and proposal-only side-effect architecture
- #388 MJ-REF-018 — Chrome/Edge side-panel companion for capture and assisted apply
- #389 MJ-REF-019 — Offline-safe browser companion and shared semantic form mapper
- #390 MJ-REF-020 — Capture and reconcile applications completed outside MJobs
- #393 MJ-REF-023 — Read-only ATS verification-code/link assistant
- #402 MJ-REF-032 — Optional least-privilege browser MCP bridge
- #403 MJ-REF-033 — Run-level stopping conditions, budgets, and browser-output validation
- #416 MJ-REF-040 — Machine-readable agent-first CLI/API contract

### Reliability, observability, operations, and governance
- #381 MJ-REF-011 — Shared AI operation budgets and explicit prompt truncation
- #382 MJ-REF-012 — Isolated agentic E2E quality monitor with baseline evidence
- #385 MJ-REF-015 — LLM token, cost, provider-attempt, and concurrency telemetry
- #386 MJ-REF-016 — Durable dead-letter queue with retry/discard workflows
- #387 MJ-REF-017 — Artifact quarantine, restore, purge, and atomic-write discipline
- #397 MJ-REF-027 — Desktop watchdog and non-admin auto-start for local MJobs
- #398 MJ-REF-028 — Encrypted provider credential store with masked client previews
- #399 MJ-REF-029 — Project-scoped Codex/agent skills for recurring MJobs workflows
- #400 MJ-REF-030 — Enforced coverage floor and provider/prompt completeness CI
- #407 MJ-REF-037 — Reference implementation provenance and license-safe inspiration policy
- #426 MJ-REF-042 — User-facing privacy, security, and data-flow transparency

### Discovery, company intelligence, and ecosystem expansion
- #391 MJ-REF-021 — Company intelligence and advisor workspace with revision history
- #392 MJ-REF-022 — Append-only Google Sheets application tracker sync
- #394 MJ-REF-024 — Grounded follow-up nudge drafter for quiet applications
- #401 MJ-REF-031 — Browser-assisted job extraction cascade with explicit provenance
- #404 MJ-REF-034 — International source country/locale capability matrix
- #405 MJ-REF-035 — Compliant JobSpy-inspired aggregator source breadth
- #406 MJ-REF-036 — Extensible source-specific metadata with canonical-field provenance

This gives MJobs 366 original requirements plus 42 additive reference-driven gaps. The catalogues are intentionally reported separately so expanding the inspiration set cannot make original-spec completion appear to regress.

## Commercial-gap extension audit — 2026-10-03

The user approved these 17 MJobs workflows. All-state GitHub issues were checked against original and reference scope; none fully owned the added workflow, so issues #454–#470 were created with stable markers. Existing canonical IDs 001–042 and closed duplicate history are unchanged. No product behavior is marked PASS by this audit.

Commercial inspiration supplied by the user: Jobscan / LoopCV / Huntr / Simplify / Careerflow / Teal / Rezi / AIApply / Sonara. These names express inspiration, not verified claims about current product implementations. No commercial code, wording, assets or algorithms were copied.

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

Each record in `planning/REFERENCE_GAP_MATRIX.json` explains the user problem and why existing overlap does not cover it, plus acceptance criteria and test strategy. Generic tailoring, autofill, tracking, provider support, outreach and question generation remain owned by existing requirements.
