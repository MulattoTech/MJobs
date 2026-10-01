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
