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
