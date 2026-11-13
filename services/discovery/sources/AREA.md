# First-party job-source adapters

Own provider collection, parsing, capabilities and bounded public GET requests.
Greenhouse currently supports explicitly configured employer boards; global
search and application submission are not supported. Requirements: MJ-REQ-016,
018-020, 063.D01, 066-068 and 228 (scoped evidence only).

Interfaces: SourceAdapter.search/fetch/normalize/health/capabilities;
SearchBatch contains per-posting SourceCompany attribution so a future global
feed can persist multiple employers in one collection. `source_adapter` is a
closed provider registry supporting Greenhouse and Lever (employer boards), Remotive
(global JSON), We Work Remotely (global RSS feed), Remote OK (global JSON), and
Arbeitnow (one latest public European feed page), and USAJOBS (recent federal IT
announcements through two public data endpoints). Both have incomplete inventory.
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

Production request-rate waits across all source adapters (Greenhouse, Remotive,
We Work Remotely, Remote OK) poll the operation cancellation callback in bounded intervals
before issuing requests. A local operation cancellation or deadline restores the source's
previous health and failure/backoff metadata; it is not recorded as evidence that the provider
is unavailable. The worker's remaining monotonic budget caps both the HTTP client's connect/read
timeout and the response-body streaming deadline (with strict 5MB payload ceilings and elapsed
transfer deadlines); the configured per-source request timeout remains the upper bound.
Active streams run with dedicated background cancellation watchers that close in-flight
responses/clients immediately when a stop is requested. This provides coherent bounded coverage
for MJ-REQ-004 across discovery collectors and application adapter form verification.

The Greenhouse adapter issues its independent board-metadata and job-inventory
GETs under a bounded per-source worker cap. Parallel requests reserve starts on
one shared rate schedule. If one request fails while its sibling observes a
local cancellation/deadline, the cancellation/deadline remains authoritative
and does not create provider-failure history. Each in-flight HTTP request polls
the worker cancellation/deadline callback; when it stops, the adapter closes
that response and client, preserves the original stop error, and leaves source
health, failure history and uncommitted jobs unchanged.

Tests: `python -m unittest tests.integration.test_greenhouse tests.integration.test_remotive tests.integration.test_wwr tests.integration.test_remoteok -v`. Live validation
is separately invoked, bounded, public and read-only; fixtures never stand in
for live evidence. Adding a provider requires official collection policy,
contract fixtures, native provenance and explicit unsupported capabilities.

## Arbeitnow niche source — MJ-REQ-017.F07

The `arbeitnow` provider uses the fixed public HTTPS JSON endpoint and the existing
collector, health, cancellation, identity and immutable-provenance path. It does
not follow pagination URLs or infer job closure from absence. Provider-native
company URL identity is scoped by the validated regional host; a display label
is not an employer-domain assertion. Source links stay aggregator routes and do
not acquire verified employer/ATS authority. API source credit, supported fields,
unsupported capabilities and validation instructions are in
`docs/sources/ARBEITNOW.md`.

## USAJOBS public federal IT source — MJ-REQ-017.F06

The default-only usajobs provider reads one recent series2210 metadata page and
at most one selected-control-number announcement-text batch. Join every native
identity field before yielding any posting. Keep raw records together, preserve
calendar-date uncertainty, and never infer inventory closure or fully remote work.
Native agency/department codes identify companies; the shared USAJOBS platform
host does not establish employer or ATS authority. The exact public-access scope,
two-request limits, manual routes and qualification state are documented in
docs/sources/USAJOBS.md.
