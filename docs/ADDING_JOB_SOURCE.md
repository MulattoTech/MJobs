# Adding a Job Source

This guide describes how to add a **read-only job-discovery source** using the existing adapter boundary. Keep provider-specific parsing and HTTP behavior in the adapter; keep shared policy and persistence in the collector. Do not add arbitrary URL fetching, credentials, browser evasion, or user application submissions to a source adapter.

## How collection is wired

The normal flow is:

1. **configure_source(...)** validates and persists the provider, tenant, polling and request policy, and the adapter's declared capabilities.
2. **source_adapter(source, transport=..., check=...)** constructs only a provider explicitly registered in **services/discovery/sources/registry.py**.
3. **collect_source(...)** owns enablement, cooldown/backoff, operation checks, source/company/job identity, normalization calls, durable raw postings and immutable snapshots, observation and health updates, and candidate persistence.
4. The adapter owns its fixed provider endpoint, public response parsing, provider identity extraction, normalization, and safe capability report.

An adapter must not write database state or update source-health persistence. The collector already applies those cross-provider responsibilities in one transaction path. Preserve the raw provider item and source identity so later audits can distinguish provider evidence from normalized values.

At this checkout, the registry includes Greenhouse, Lever, Remotive, We Work Remotely (WWR), and Remote OK (including the **remote_ok** alias). Registry membership is an explicit allowlist; unknown provider names fail with **SOURCE_UNSUPPORTED**.

## Adapter interface

Implement the protocol in **services/discovery/sources/contract.py**:

    class SourceAdapter(Protocol):
        def search(self) -> SearchBatch: ...
        def fetch(self, job_id: int) -> dict: ...
        def normalize(self, raw: dict) -> dict: ...
        def health(self) -> str: ...
        def capabilities(self) -> SourceCapabilities: ...

The shared immutable data types are:

- **SourceCompany(identity_parts, name, domain)**: provider-stable company identity and display metadata. Use stable identity parts; do not use a display-name guess as proof that two companies are identical.
- **SourceItem(raw, company)**: one unmodified provider item and its company attribution. This is important for global feeds that contain multiple employers.
- **SearchBatch(endpoint, items, source_company=None, complete_inventory=False)**: canonical endpoint observed, item tuple, optional single-company attribution, and an explicit completeness assertion.
- **SourceCapabilities(strategy, features)**: declared **CollectionStrategy** and a set of **SourceCapability** values. Report only supported behavior. Do not claim direct application, status, salary, or detail support just because fields happen to appear in a payload.

For a single-company board, set **SearchBatch.source_company** when the whole batch has one authoritative company. For multi-company feeds, put the appropriate **SourceCompany** on every **SourceItem**. Return **complete_inventory=True** only when the fetch is a complete, authoritative inventory for that source scope; the collector uses it to reconcile missing postings.

Keep the adapter's constructor compatible with registry construction. Existing adapters accept an optional injected HTTP transport and a **check** callback for cancellation/deadline checks, plus the source's validated **request_timeout**, **rate_limit**, and **concurrency** policy.

## Normalization and identity

Treat provider payloads as untrusted input:

- Validate required fields and native posting identifiers before returning a normalized record. Raise **SourceError(code, retry_seconds=...)** with a stable, non-sensitive code for provider-data or transport failures.
- Return the provider-neutral posting fields consumed by the collector, including a stable **requisition**, title, readable description, location/remote metadata, skills where available, **application_url**, and structured **attributes**. Reuse shared helpers under **services/discovery/search_semantics.py** and related normalizers rather than creating provider-specific variants of common semantics.
- Keep **item.raw** unchanged. Store source attribution and native identifiers in the adapter's result/attributes; never use a mutable display label as the posting key.
- Preserve application links as provider evidence, not permission to submit. Validate links against the provider's documented HTTPS origins and tenant/board scope; unsupported or unsafe links should be unavailable rather than repaired into a different origin.
- Preserve each posting's company attribution. Feeds may reuse native requisition IDs across employers; identity must not collide across companies.
- Do not claim the feed is complete when pagination, truncation, or provider limits make coverage uncertain.

The collector records raw payloads and immutable posting snapshots separately from normalized values. Normalization must not discard the original evidence or fabricate missing values.

## Rate limits and bounded requests

Source policy is configured centrally by **configure_source**; current supported bounds are:

| Policy | Supported value |
| --- | --- |
| Poll interval | 300–604800 seconds |
| Per-source concurrency | 1–8 |
| Request timeout | 2–60 seconds |
| Rate limit | 1–600 requests per minute |
| Jitter | 0–300 seconds |

The adapter's **rate_limit** value is requests per minute; production adapters reserve request starts on a shared schedule with a minimum interval of **60 / rate_limit** seconds. Waits must poll the operation's **check** callback so cancellation or the worker deadline can stop before egress. Do not create an unbounded thread/task per posting or bypass the configured concurrency.

For production HTTP, use the provider's fixed documented endpoint, HTTPS, certificate verification, **trust_env=False**, and disabled redirects. Do not accept a caller- or payload-selected destination. Bound connect/read time by the smaller of the configured timeout and remaining operation budget; stream the response under an elapsed-time deadline and the existing 5 MB response limit. Close active responses/clients on cancellation or timeout. A local cancellation/deadline is not evidence that the provider is down.

Honor provider **Retry-After** where valid and classify rate limiting as **SOURCE_RATE_LIMITED**; never hammer the endpoint after a 429. Avoid logging raw provider bodies, applicant data, secrets, or full exception text that may contain response content.

## Health reporting

Expose adapter-local status through **health()**; initialize it to **NOT_CHECKED**, update it only from observed request outcomes, and protect concurrent reads/writes if the adapter makes requests in parallel. The collector owns persisted health:

- Collection start is **COLLECTING**.
- A successful collection pass is **HEALTHY**; a pass that rejected malformed postings is **PARTIAL**.
- Provider/network/rate failures become persisted **FAILED** or **RATE_LIMITED**, with a stable error code and backoff.
- Local **OperationCancelled** or **OperationTimedOut** restores prior health/failure/backoff metadata. Do not turn a local stop, authority change, database error, or parser programming error into proof of provider unavailability.

Test health state as well as the raised error/result. Failed collection must not persist partial jobs; cancellation must not poison source availability or create provider-failure history.

## Fixtures and tests

Use synthetic fixtures and deterministic transports. Existing adapter integration tests use **httpx.MockTransport** to assert the request method, fixed host/path, and bounded payload shape. Do not make fixture tests call a live provider or use captured credentials, applicant data, or private employer records. Keep live-source validation separate, bounded, public, and explicitly invoked; mock evidence is not live-provider evidence.

For each adapter, cover:

- capability declarations and registry construction, including unsupported capabilities/providers;
- search batch endpoint, raw payload preservation, company attribution (including multi-company cases), and inventory completeness;
- normalization of valid data and rejection of missing/malformed identifiers, dates, locations, URLs, or types;
- unsafe URL variants, redirects, unexpected content types, invalid JSON/XML, 429/5xx, and oversized or slow responses;
- rate-limit scheduling, concurrency bounds, operation cancellation/deadline during a wait and during active I/O;
- health/backoff classification, no partial persistence after failure, and stable source/job identity after repeat collection.

Useful targeted suites from repository root are:

    python -m unittest tests.integration.test_greenhouse tests.integration.test_remotive tests.integration.test_wwr tests.integration.test_remoteok -v
    python -m unittest tests.integration.test_source_adapter_connect_cancellation tests.integration.test_source_http_socket_budget -v
    python -m pytest -q tests/unit/test_lever_source.py tests/unit/test_remotive_url_contract.py

Keep fixtures narrowly scoped to the contract they prove. Run the relevant adapter tests after changing parsing or transport behavior; run focused catalog, progress, and whitespace checks for documentation or policy changes. Report skipped or unavailable checks as **NOT_RUN**, not as passes.

## Adding a provider checklist

1. Confirm the official public endpoint and collection policy, allowed request method, licensing/terms, pagination and rate limits.
2. Implement the typed adapter and stable company/posting identity.
3. Register only the explicitly supported provider in **registry.py**; reject unknown providers.
4. Add synthetic response fixtures and positive/negative contract tests before enabling collection.
5. Verify capabilities, rate/cancellation behavior, health classification, and persistence/readback through the provider-neutral collector.
6. Update **services/discovery/sources/AREA.md** and this guide if the shared contract or supported provider list changes.
7. Keep live validation separate and never describe synthetic fixtures as live provider acceptance.
