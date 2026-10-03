# Company Discovery Area

Owns company identity resolution, trigger policy, official career-source discovery, automatic source enrollment, and immediate employer enumeration.

## Boundaries
- API/scheduler may enqueue discovery but do not perform external discovery I/O.
- External company discovery executes only on the discovery worker.
- Company identity decisions must return confidence and evidence.
- Low-confidence/unsupported infrastructure must surface review/block state rather than guess.
- Provider-specific ATS detection/collection remains in `services/ats_detection` / source adapters as those requirements activate.
- Never bypass access controls, anti-bot protections, CAPTCHA, or authentication.
- Preserve all source/provenance history when employer infrastructure changes.

## Current slice
MJ-PR-016 establishes the durable trigger/resolver/flywheel contract and proves it against the explicit local fixture. Production official-domain/career crawling and additional ATS detectors are separate tracked requirements.

## Companies/Sources read model and controls
`list_company_source_view` projects persisted source-specific feed postings and current-profile match records without treating candidate domains as official or inferring missing scheduling. Explicit pin/enabled/watch choices remain independent. New discovery events carry `company_source_id`; unlinked historical events remain unavailable. `apps/web/src/company-sources.tsx` renders this contract with unsupported actions clearly disabled.


## Source-quality observations

MJ-REQ-083 remains PARTIAL. `quality.py` stores source-attributed observations and exposes bounded API scores from `quality_score.py`. Configured collectors explicitly link company-source rows; their statistics retain board-wide scope. Feed refresh failures are attributed only to enrolled exact portals. Seven equal-weight components retain unknown values and coverage; collection freshness is not job age and historical uptime means observed attempt availability. Neither quality nor collection success verifies employer identity or authorizes submission. No history is fabricated on migration. Populated downgrade refuses evidence loss. Application-link accuracy remains follow-up work. Quality evidence is scoped to the observed endpoint; legacy observations without an endpoint remain unknown for endpoint-specific comparisons. Authority-first duplicate selection is owned by services/dedupe/source_values.py.
