# ATS Detection Area

Owns provider identification from public career-page evidence. Detection does
not imply job enumeration, authentication, or application automation support.

## Boundaries
- Consume only page content and redirect metadata fetched by a bounded,
  same-origin approved discovery transport.
- Return confidence and evidence categories; ties and weak evidence require
  review rather than a provider guess.
- Treat HTML, script URLs, forms, and embedded configuration as untrusted data.
- Provider detection is independent from `services/discovery/sources`, which
  owns job collection adapters.
- Do not submit forms, execute page scripts, or bypass access restrictions.

## Current slice
MJ-PR-018 adds deterministic evidence signatures for common ATS families and
custom-domain attribution. Search is disabled by default and accepts injected
provider adapters; no external search service is configured in this slice.
