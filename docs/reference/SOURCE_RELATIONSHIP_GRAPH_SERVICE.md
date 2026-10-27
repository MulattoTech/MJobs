# Maintainer reference: source relationship graph service

> **Reference baseline:** [b73750e37c487a008dfdd2277440a2559d98ecf4](https://github.com/MulattoTech/MJobs/commit/b73750e37c487a008dfdd2277440a2559d98ecf4). All source/test line ranges refer to this revision, not floating main. Reviewed draft origin: 2f01b876762fcbdd70f11e69865a48257503af93; cited blobs are unchanged at this baseline. Static inspection is not runtime validation or requirement acceptance. Examples and maintainer cautions are guidance, not additional API behavior.

[Read the primary source](https://github.com/MulattoTech/MJobs/blob/b73750e37c487a008dfdd2277440a2559d98ecf4/services/operations/source_relationship_graph.py).


**Source path:** `services/operations/source_relationship_graph.py`
**Source commit:** `b73750e37c487a008dfdd2277440a2559d98ecf4`
**Source blob:** `7115f4a9bb7381e4433c98485cbad3118e2c8041`

This service builds a read-only graph from persisted records visible through one profile's links. It does not mutate data or infer that a domain/source is official.

## Entry point and visibility

`read_source_relationship_graph(store, profile_id)` first resolves the profile, then reads related rows through a SQLAlchemy connection (source lines 40-224). A job is visible if the profile has a match row or application. A company is visible if referenced by one of those jobs or by this profile's `COMPANY_DISCOVERED`/`COMPANY_SOURCE_ENROLLED` audit events (lines 10, 46-86). Postings and routes are restricted to visible jobs. A source appears only when enrolled in the profile's audit history or linked by a persisted value-candidate row to a posting for a visible job, and its company is itself visible (lines 87-117). A source-to-posting edge requires that exact candidate link (lines 119-132). Applications connect only when their referenced route exists and both route/application refer to the same job (lines 184-212).

This creates an important boundary: company-only audit visibility does not reveal all company jobs, sources, routes, or applications. Those objects are separately gated by the profile-visible job/application and source-link queries.

## Output shape and determinism

The return value is `{"nodes": [...], "edges": [...]} `. Each node has `id`, `type`, `label`, and `metadata`; each edge has `id`, `source`, `target`, and `type` (source lines 22-38). Node types are `company`, `company_job_source`, `canonical_job`, `job_posting`, `application_route`, and `application`. Edge types are `company_source`, `job_posting`, `source_posting`, `job_route`, and `route_application` (lines 134-218). IDs are namespaced as `type:identifier`; edge IDs encode type and endpoints. Nodes and edges are deterministically sorted and duplicate edge IDs collapse before return (lines 219-224).

Labels and metadata are constructed from an explicit allowlist of fields. `_safe_text` applies Unicode NFKC, replaces non-printable characters with spaces, collapses whitespace, truncates to a field-specific bound, and returns `None` for unusable text (lines 13-20). Company domain is casefolded and has a trailing dot removed; timestamps and labels have smaller maximum lengths where specified (lines 135-182, 193-207). The service does not copy arbitrary row payloads or descriptions into graph nodes.

## Dependencies and errors

The service depends on SQLAlchemy `select` and persistence table models (imports at lines 3-7). It has no HTTP types and does not translate exceptions. An unknown profile propagates the error from `store.profile(profile_id)`; SQL/database failures likewise propagate. The API wrapper separately converts a missing profile to a 404 `SOURCE_RELATIONSHIP_GRAPH_PROFILE_NOT_FOUND` and validates/projects returned nodes/edges (apps/api/source_relationship_graph.py lines 36-107). A valid profile with no visible relationships returns empty arrays, not a missing-profile error; this is asserted by the unit/API tests.

## Safe synthetic use

A fixture-only caller can use the service contract like this:

```python
graph = read_source_relationship_graph(store, "fixture-profile")
assert set(graph) == {"nodes", "edges"}
```

This assumes a migrated temporary Store containing synthetic profile, match/application, company/source/posting and route fixtures; it was not executed here. Unit tests verify exact edge topology, sanitization, deterministic repeated results, profile isolation, and empty-profile output (tests/unit/test_source_relationship_graph.py lines 89-131). Integration tests verify read-only GET behavior, profile isolation, empty graph and missing-profile HTTP behavior (tests/integration/test_source_relationship_graph.py lines 108-154).

## Existing documentation coverage

The requirement section in `docs/spec/areas/operations_ux_ci.md` lines 285-303 gives the conceptual Company/Source/Posting/Job/Route/Application diagram. README lines 71-73 shows a screenshot. Neither describes service-level profile visibility, output schema, edge inclusion, sanitization, ordering, or error boundaries. This note supplements those brief references rather than duplicating the diagram.

**Validation:** exact pinned source/blob, API wrapper, focused tests and spec excerpt inspected; tests and CI were not run (NOT_RUN).
