# First-party job-source adapters

Own provider collection, parsing, capabilities and bounded public GET requests.
Greenhouse currently supports explicitly configured employer boards; global
search and application submission are not supported. Requirements: MJ-REQ-016,
018-020, 063.D01, 066-068 and 228 (scoped evidence only).

Interfaces: SourceAdapter; Greenhouse; configure_source and collect_source.
Persist job_sources, company sources, canonical jobs/routes, raw postings and
immutable snapshots, profile-version matches and audits. API/worker code calls
this boundary without containing provider parsing. No credentials, browser
evasion, user application POSTs or arbitrary URL fetching belong here.

Tests: `python -m unittest tests.integration.test_greenhouse -v`. Live validation
is separately invoked, bounded, public and read-only; fixtures never stand in
for live evidence. Adding a provider requires official collection policy,
contract fixtures, native provenance and explicit unsupported capabilities.
