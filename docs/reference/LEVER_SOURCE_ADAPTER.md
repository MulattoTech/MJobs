# Lever source maintainer reference

**Source path:** `services/discovery/sources/lever.py`
**Source commit:** `2f01b876762fcbdd70f11e69865a48257503af93`
**Source blob:** `1e2a85d860bad1141e15038742908a9e9d1022d1`

This reference describes the implementation at that exact source revision. The AST/docstring inventory is a navigation aid, not evidence of a defect. The repository's `docs/ADDING_JOB_SOURCE.md` already documents the shared read-only adapter contract, collection boundary, safety policy, and test approach; this note adds Lever-specific behavior rather than restating that guide. (The link is contextual to the source checkout.)

## How it is entered and configured

The explicit registry exposes provider key `lever` and constructs `Lever` with a required tenant plus the injected transport/check callback and source request policy (registry defaults: 10-second request timeout, 60 requests/minute, concurrency 1) [registry.py:19-38]. `Lever` subclasses the existing `Greenhouse` transport implementation, forwards keyword configuration, and keeps both parsed region/site and normalized tenant identity [lever.py:63-72].

A tenant is a string of `global:<site>` or `eu:<site>`. The region is case-folded; the site is case-folded and must match the bounded site pattern. Invalid forms raise `ValueError("SOURCE_INVALID_TENANT")` [lever.py:20-35]. The API endpoint is fixed by region: `https://api.lever.co/v0/postings/<site>` for global and `https://api.eu.lever.co/v0/postings/<site>` for EU [lever.py:22-23,38-40].

Safe synthetic construction follows the existing unit-test helper pattern; `MockTransport` prevents provider egress:

```python
adapter = source_adapter(
    {"provider": "lever", "tenant": "eu:example"},
    transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
)
batch = adapter.search()
```

The helper uses `source_adapter` with a deterministic `httpx.MockTransport` [tests/unit/test_lever_source.py:51-53]. Do not substitute a live transport merely to try this example.

## Public adapter operations

- **`search()`** temporarily wraps the inherited `check` with a 120-second monotonic inventory deadline. It also respects an inherited `remaining_seconds` callback when available and restores the original check in `finally`; expiry raises `SourceError("SOURCE_TIME_LIMIT")` [lever.py:24-26,74-94].
- **Inventory request/pagination:** the adapter issues GET requests against its fixed endpoint with `mode=json`, `skip=page*100`, and `limit=100`. Every response must be a list of at most 100 entries. A short page ends inventory; ten full pages do not prove exhaustion and raise `SOURCE_JOB_LIMIT`. Duplicate normalized requisition IDs raise `SOURCE_DUPLICATE_JOB_ID`; malformed page shape raises `SOURCE_INVALID_FORMAT`. No partial `SearchBatch` is returned on those failure paths [lever.py:96-118]. A successful batch has the canonical endpoint, raw `SourceItem`s, a company identity keyed by `('lever', tenant)`, and `complete_inventory=True` [lever.py:96-115].
- **`fetch(job_id)`** accepts a UUID-shaped string, requests that lower-cased ID, normalizes the response to verify it refers to the requested requisition, then returns the original raw mapping. Invalid identifiers raise `ValueError("SOURCE_INVALID_JOB_ID")`; mismatched response IDs raise `SourceError("SOURCE_JOB_ID_MISMATCH")` [lever.py:120-127].
- **`normalize(raw)`** converts one untrusted Lever posting into provider-neutral requisition, title, description, skills, location, remote type, application URL, and attributes. It requires a mapping, UUID-like `id`, nonblank `text`, and dictionary `categories`; invalid records raise `SOURCE_INVALID_JOB` [lever.py:129-138]. The hosted URL must be HTTPS on the tenant's exact regional jobs host and site, with a UUID path. An optional apply URL must satisfy the same tenant/posting constraints. Rejected links produce `COMPANY_ATS_JOB_TENANT_MISMATCH`; `listing_url` returns `None` for noncanonical/unsafe links and canonicalizes accepted `/apply` URLs to the posting URL [lever.py:43-60,134-138].

## Normalization details and bounds

The title is plain text capped at 500 characters. Description text combines description/descriptionPlain, up to 50 list entries, and additional/additionalPlain; each part is capped at 500,000 characters before structured-text conversion, whose result is capped at 50,000 [lever.py:139-146]. Location is plain text capped at 1,000 characters, falling back to `Unknown`. Location and description feed shared location classification; recognized `workplaceType` values `remote`, `hybrid`, and `on-site` override the resulting remote classification to `REMOTE`, `HYBRID`, and `ONSITE` respectively [lever.py:147-153]. Shared parsers derive skills, experience/title classifications, education, requirements, negative conditions, and description-based salary [lever.py:153-159,168-182].

A salary range is added only when both endpoints are finite numeric values in `[0, 1e9)`, with minimum no greater than maximum. Only `per-year`/`year` and `per-hour`/`hour` map to known periods; other intervals become `UNKNOWN`. Currency text is capped at eight characters, and the original range is retained as salary evidence [lever.py:160-167]. Output attributes preserve provider, tenant, regional instance/site and native posting IDs; `reported_posted_at` is `None`, `submission_capability` is `MANUAL_ONLY`, and `fictional` is `False` [lever.py:168-184]. The source does not implement application submission.

## Existing test coverage and maintainer pointers

The unit suite uses synthetic postings and mock HTTP transport. It checks exact regional API hosts and query parameters, normalization/capabilities, multi-page completion, later-page failure, page-cap and duplicate failure, and cross-region/site/URL rejection [tests/unit/test_lever_source.py:15-21,51-112]. It also checks that the inventory deadline closes an in-flight stream and restores the inherited check callback [tests/unit/test_lever_source.py:25-42].

For generic adapter responsibilities and the repository's targeted test command, use `docs/ADDING_JOB_SOURCE.md:3-16,20-38,82-101`. The Lever-focused existing test command there is `python -m pytest -q tests/unit/test_lever_source.py tests/unit/test_remotive_url_contract.py` [docs/ADDING_JOB_SOURCE.md:95-101]. This task inspected source and references only; no tests were run, and no acceptance or live-provider behavior is claimed.
