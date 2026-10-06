# First-party job-source adapters

Own provider collection, parsing, capabilities and bounded public GET requests.
Greenhouse currently supports explicitly configured employer boards; global
search and application submission are not supported. Requirements: MJ-REQ-016,
018-020, 063.D01, 066-068 and 228 (scoped evidence only).

Interfaces: SourceAdapter.search/fetch/normalize/health/capabilities;
SearchBatch contains per-posting SourceCompany attribution so a future global
feed can persist multiple employers in one collection. `source_adapter` is a
closed provider registry; Greenhouse is the only production implementation.
The legacy Greenhouse.collect method delegates to search for compatibility.
configure_source and collect_source own policy and provider-neutral persistence.
Persist job_sources, company sources, canonical jobs/routes, raw postings and
immutable snapshots, profile-version matches and audits. API/worker code calls
this boundary without containing provider parsing. No credentials, browser
evasion, user application POSTs or arbitrary URL fetching belong here.

Stable Greenhouse company/job/route IDs and immutable posting snapshots must be
preserved across adapter changes. An adapter supplies validated company identity,
source endpoint and raw postings; the collector never parses provider payloads.
For multi-company feeds, raw posting identity also includes company identity so
two employers can reuse the same requisition without overwriting provenance.

Greenhouse's production request-rate wait polls the operation cancellation
callback in bounded 250 ms intervals before issuing the next request. A local
operation cancellation or deadline restores the source's previous health and
failure/backoff metadata; it is not recorded as evidence that the provider is
unavailable. This is scoped coverage for MJ-REQ-004; provider timeouts, worker
deadlines, and the other external operation categories still require their own
acceptance evidence.

The Greenhouse adapter issues its independent board-metadata and job-inventory
GETs under a bounded per-source worker cap. Parallel requests reserve starts on
one shared rate schedule. If one request fails while its sibling observes a
local cancellation/deadline, the cancellation/deadline remains authoritative
and does not create provider-failure history.

Tests: `python -m unittest tests.integration.test_greenhouse -v`. Live validation
is separately invoked, bounded, public and read-only; fixtures never stand in
for live evidence. Adding a provider requires official collection policy,
contract fixtures, native provenance and explicit unsupported capabilities.
