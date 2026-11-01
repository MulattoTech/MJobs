# Adding a company job source

This guide covers MJ-REQ-283: company resolution, career discovery, ATS detection,
tenant extraction and job enumeration. A company source binds an employer to a
specific observed career endpoint. A detected provider name or a plausible domain
does not establish that binding.

## Follow the existing pipeline

| Stage | Owner | Output and boundary |
| --- | --- | --- |
| Admission and identity | [requests.py](../services/company_discovery/requests.py), [resolution.py](../services/company_discovery/resolution.py), [public_resolution.py](../services/company_discovery/public_resolution.py) | Durable request, profile/version, candidate and evidence; ambiguity retains review |
| Career discovery | [flywheel.py](../services/company_discovery/flywheel.py), [career_pages.py](../services/company_discovery/career_pages.py) | Bounded same-origin career/sitemap candidates |
| Public transport | [public_client.py](../services/company_discovery/public_client.py), [fetch_policy.py](../services/company_discovery/fetch_policy.py) | Robots-aware, origin-bound HTTPS reads with cancellation, DNS/socket and byte limits |
| ATS detection | [detector.py](../services/ats_detection/detector.py) | Provider, confidence and evidence from supplied page/URL observations; no network call |
| Tenant enrollment | [board_links.py](../services/ats_detection/board_links.py), [ats_enrollment.py](../services/company_discovery/ats_enrollment.py), [sources.py](../services/company_discovery/sources.py) | Exact provider/tenant and persisted employer-source relationship |
| Enumeration | [source registry](../services/discovery/sources/registry.py), [collector](../services/discovery/sources/collect.py), [endpoints.py](../services/company_discovery/endpoints.py) | Raw and normalized postings, immutable provenance and canonical identities |
| Recurrence | [monitoring.py](../services/company_discovery/monitoring.py), [scheduler.py](../services/discovery/scheduler.py) | Authorized, bounded outbox work; existing enablement and review decisions survive |

API and scheduler code admit durable work; the company-discovery worker performs
external reads. Reuse the worker's `check` callback and transaction guards instead
of fetching from a request handler or adding a second importer.

## Company resolution

`resolve_company(store, candidate)` returns a stable candidate identity, confidence,
evidence and whether it is new. Verified domains and aliases, normalized names and
current authorized ATS sources are different kinds of evidence. A normalized name
match alone must not establish ownership of a public website or portal. Preserve
separate parent/subsidiary records unless the explicit relationship is known.

Public reads use `resolve_public_company`: it binds an existing employer only to
qualifying, unambiguous domain evidence. Otherwise it creates an origin-specific
candidate with `CANDIDATE_DOMAIN_UNVERIFIED`. A claimed employer name or tenant
must not attach an unrelated public site to a known employer.

There are two additional, explicit authorization paths. A reviewed discovery
request can carry `USER_CONFIRMED_DOMAIN`, tied to its live request, source revision
and audit record. The narrowly supported [domain acquisition path](../services/company_discovery/domain_acquisition.py)
can corroborate a configured Greenhouse job link against an employer page that
independently identifies the exact job and links back to its exact native posting.
Neither a supplied URL nor JSON-LD by itself constitutes that proof. Revalidate
these records before fetch and during enrollment/import; do not copy an old
`verified` flag into a new source.

## Career discovery and transport

Start from the authorized origin. `internal_career_links` extracts same-origin
navigation, footer and structured-data links; `common_career_routes` supplies
bounded candidates. `sitemap_locations` and `parse_sitemap` handle advertised
same-origin sitemap/index entries. The pure parsers do not authorize a fetch.

Use `PublicCompanyClient` through the existing flywheel. It rejects unsafe targets,
pins public DNS resolutions to the connection, verifies TLS and hostname, enforces
same-origin redirects, respects robots policy and limits requests, response bytes,
elapsed time and cancellation. Never replace it with unrestricted `httpx.get`,
browser navigation, proxy inheritance or a caller-selected arbitrary client in
production. Fixture transports belong only in tests with synthetic responses.

Search results and career candidates remain evidence. A discovery failure must
retain the earlier company/source/history and expose a stable error or review
state. If an employer changes endpoints, do not silently re-parent the old source
or treat the new endpoint as authorized because its display label is similar.

## ATS detection and tenant extraction

Call `detect_ats(visible_careers_url, html, redirect_chain=...)` on already observed
data. It combines host, link, script, form, URL and structured signals, returning
confidence and candidates. A weak path pattern alone is insufficient. Detection
does not imply that enumeration, form interaction or submission is implemented.

The current employer-specific collection adapters are **Greenhouse and Lever**.
Extract their tenant with the structural parsers in `board_links.py`, then require
one unambiguous tenant before enrollment. Never infer it from employer display
name, free-form page text or an untrusted script string. Lever identities are
region-qualified (`global:tenant` or `eu:tenant`); preserve that distinction.

This example performs only local parsing and no HTTP requests:

```python
from services.ats_detection.board_links import greenhouse_board_tenants, lever_board_tenants
from services.ats_detection.detector import detect_ats

career_url = "https://careers.example/careers"
markup = '<a href="https://boards.greenhouse.io/example">Open roles</a>'
assert greenhouse_board_tenants(career_url, markup) == {"example"}
assert detect_ats(career_url, markup).provider == "greenhouse"
assert lever_board_tenants(
    career_url, '<a href="https://jobs.eu.lever.co/example">Open roles</a>'
) == {"eu:example"}
```

`enroll_public_ats` rechecks employer authority and takes the existing source-host
and row locks before storing a tenant and source. It preserves an existing
collector's cadence, rate limits and disablement. Conflicting owners, ambiguous
tenants or revoked authority require review; they must not become a second owner
of the same board. Unsupported ATS providers retain explicit unsupported/review
status until their complete collection boundary is implemented.

## Job enumeration and persistence

For an ATS API source, extend the typed source contract and immutable registry
described in [Adding a job source](ADDING_JOB_SOURCE.md). Enrollment delegates to
`configure_source(..., preserve_existing=True)` and `collect_source`; it does not
write jobs through a parallel schema. Preserve provider, regional tenant/native
posting identity, raw snapshots, normalized attributes and application routes.
Set inventory completeness only when the provider result actually establishes it.

For supported public RSS/Atom or JSON feeds, the existing endpoint parser and
feed pipeline retain same-origin policy. Generic feed discovery does not fabricate
an ATS tenant. Candidate-domain sources need their explicit review/monitoring
authorization; recurring collection is not granted by a historical enabled flag.

Collection, immutable observations and completion receipts must agree within the
existing transaction. Retained observations may be reused when source policy
defers a refresh, but they must remain labeled retained/deferred rather than fresh
or complete. Restart/replay must preserve the durable receipt, not duplicate jobs
or refetch after an already committed import.

## Tests and delivery checklist

Use synthetic employer names, `.example` origins, `httpx.MockTransport` and the
existing fixture client. No real employer account, provider request or application
submission is needed to verify these contracts.

```sh
python -m pytest -q tests/unit/test_company_resolution.py tests/unit/test_company_resolution_sustain_217.py tests/unit/test_company_source_contract_sustain_224.py
python -m pytest -q tests/integration/test_public_company_discovery.py tests/integration/test_company_sources.py tests/integration/test_company_discovery_sitemaps.py tests/integration/test_company_discovery_sitemap_safety.py
```

For each added provider, prove exact identity and endpoint shape; rejected malformed
or ambiguous tenants; bounded enumeration; normalization and raw preservation;
unsafe/redirected response rejection; cancellation and failure classification;
disablement and authority revocation; repeated collection/restart idempotency;
and no partial writes after a failed import. Extend the exact registered-provider
coverage in `test_company_source_contract_sustain_224.py` deliberately when the
supported employer-specific registry changes.

Keep migrations and PostgreSQL locking verification separate from SQLite unit
evidence. Record the actual commands, revision and results; mocked HTTP is not live
provider acceptance. Update the [company discovery area contract](../services/company_discovery/AREA.md)
when behavior changes, including any remaining unsupported cases.
