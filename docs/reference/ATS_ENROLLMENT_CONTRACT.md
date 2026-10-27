# Maintainer reference: ATS enrollment

## Immutable source reviewed

- **Source path:** `services/company_discovery/ats_enrollment.py`
- **Source commit:** `2f01b876762fcbdd70f11e69865a48257503af93`
- **Full source blob SHA:** `aa86ff2e86a7f5d7458378a43ca33c39fc763c3b`
- **Review method:** static reading of the immutable source and nearby tests/docs. No test, verifier, network, or provider execution was performed for this reference.

## Purpose and boundary

This module binds a supported public ATS board to already established employer authority, persists the company/source/collector relationship, and either reuses retained observations or invokes the normal collector. It does not itself establish employer identity from a company name or ATS tenant. The area contract says automatic enrollment requires an independently verified official career page, preserves existing collector policy and source disablement, and must stop on ambiguous tenants, revoked authority, or conflicting ownership (`services/company_discovery/AREA.md:20-24`). The architecture summarizes the same authority/review boundary (`docs/ARCHITECTURE.md:149-164`).

The deterministic inventory's one undocumented public symbol is the compatibility alias `enroll_greenhouse = enroll_public_ats` (`services/company_discovery/ats_enrollment.py:315-316`). The four public functions it exposes already have inline docstrings.

## Public entry points

### `greenhouse_tenant(career_url, html) -> str`

Parses exact Greenhouse board URLs from fetched career-page markup by delegating to `greenhouse_board_tenants`. Exactly one tenant must be found; malformed or ambiguous evidence is translated to `PublicFetchError('COMPANY_ATS_TENANT_REVIEW_REQUIRED')` (`services/company_discovery/ats_enrollment.py:19-27`).

Inputs:

- `career_url`: the URL against which board links in `html` are interpreted by the board-link parser.
- `html`: fetched employer career-page markup.

Output: the sole normalized tenant string, removed from the parser's result set.

The unit contract accepts supported official hyperlinks/embeds and rejects script text, attacker hosts, userinfo, HTTP, disallowed ports, traversal, conflicting query values, and multiple tenants (`tests/unit/test_greenhouse_enrollment.py:8-29`).

### `bound_company_source(store, connection, source, *, lock=False) -> dict | None`

Validates that a configured collector is bound to one automatically discovered company source and to current employer-domain authority (`services/company_discovery/ats_enrollment.py:47-112`).

Inputs:

- `store`: persistence store used by public-company resolution.
- `connection`: an active SQLAlchemy connection. The function does not open or commit its own transaction.
- `source`: mapping with at least `id`, `provider`, and `tenant`; supported bound providers are `greenhouse` and `lever`.
- `lock`: when true, acquires ingestion, domain/source-host, and row locks in the module's prescribed order, then re-reads and compares binding-critical fields (`services/company_discovery/ats_enrollment.py:61-85`).

Output:

- a plain `dict` for the single valid enabled binding;
- `None` when no automatically discovered binding exists, preserving legacy explicitly configured collector identity (`services/company_discovery/ats_enrollment.py:86-87`).

Fail-closed errors are `SourceError` codes, including:

- `COMPANY_ATS_BINDING_REVIEW_REQUIRED` for ambiguous, changed, cross-company, mismatched-provider/tenant/endpoint/strategy, or invalid Lever binding;
- `COMPANY_SOURCE_DISABLED` for a disabled company source;
- `COMPANY_DOMAIN_REVIEW_REQUIRED` when current resolution no longer corroborates the same company with verified or user-confirmed domain evidence.

`ValueError` raised by domain authorization helpers is converted to `SourceError` with the helper's message (`services/company_discovery/ats_enrollment.py:66-80,99-111`).

### `collector_view(store, connection, source) -> dict | None`

Projects the persisted collector state for the company/source read model without inventing a next-run operation or time (`services/company_discovery/ats_enrollment.py:115-136`).

It returns `None` unless the source is automatically discovered and has both a collector ID and career URL. Otherwise it loads the collector and returns:

- `id`, `poll_interval`, and `health_status` from the collector;
- `status`: `ACTIVE`, `REVIEW_REQUIRED`, `NO_SCHEDULED_SEARCH`, or `PAUSED`;
- `identity_basis`: `USER_CONFIRMED_DOMAIN` when an identity authorization exists, otherwise `VERIFIED_DOMAIN`.

Binding or public-fetch failures are represented as `REVIEW_REQUIRED` rather than propagated. An enabled source without a qualifying enabled search profile becomes `NO_SCHEDULED_SEARCH`; disabled source or collector state wins as `PAUSED` (`services/company_discovery/ats_enrollment.py:122-136`). Database lookup failures such as a missing collector row are not caught here.

### `enroll_public_ats(...) -> dict`

Signature and orchestration are at `services/company_discovery/ats_enrollment.py:221-312`.

Required inputs:

- `store`: persistence store with the relevant schema/workspace already configured.
- `candidate`: company candidate mapping accepted by `resolve_public_company` when independent verification must be refreshed.
- `resolved`: prior resolution containing at least `company_id`.
- `career_url`, `html`: the fetched career page and its markup.
- keyword-only `profile_id` and `check` callback. `check()` is invoked before the enrollment transaction and again after it (`services/company_discovery/ats_enrollment.py:239-241,302-302`).

Optional inputs:

- `profile_version`: forwarded to `collect_source`.
- `enrollment_guard`: invoked inside the enrollment transaction and forwarded as the import guard.
- `preserve_source_id`: requires the upserted source ID to remain unchanged.
- `identity_confirmation`: admission evidence for a user-confirmed employer; it grants no authority by itself and must match the live guard result.
- `provider`: `greenhouse` by default, or `lever`; every other value fails with `COMPANY_ATS_ENUMERATION_UNSUPPORTED` (`services/company_discovery/ats_enrollment.py:227-238`).

Effects and dependencies:

1. Extract one canonical provider tenant and construct its provider endpoint.
2. In one store transaction, acquire the company ingestion lock, refresh or verify company identity, lock source hosts, check live authorization, and create or reuse the collector without changing its existing policy (`services/company_discovery/ats_enrollment.py:241-266`).
3. Refuse cross-company ownership or unsupported Lever migration, register the ATS tenant, upsert the company source, preserve explicit source identity/disablement, store the collector binding and optional authorization digest, revalidate the binding under locks, and append a `COMPANY_SOURCE_DISCOVERED` audit event (`services/company_discovery/ats_enrollment.py:267-301`).
4. Outside that transaction, return eligible retained observations or call the normal `collect_source` path with the guard (`services/company_discovery/ats_enrollment.py:302-312`).

Return shapes:

- retained/deferred path: company/source/provider/tenant/career/endpoint fields plus the inventory and refresh fields produced by `_retained_native_inventory`;
- immediate collection path: company/source/provider/tenant/career/endpoint fields plus `jobs_enumerated` and `jobs_imported`, both set from `result['jobs_seen']`.

Important errors include tenant review/unsupported provider, changed authorization, domain review, disabled source, binding review, migration review, and any errors propagated by persistence, resolution, tenant registration, source upsert, or collection. The transaction provides the atomic boundary for enrollment writes; collection happens afterward.

### `enroll_greenhouse`

This is an alias, not a wrapper. It points to the same function object as `enroll_public_ats` and therefore retains the default `provider='greenhouse'` contract (`services/company_discovery/ats_enrollment.py:315-316`). New provider-aware callers should use `enroll_public_ats`; existing internal Greenhouse callers may continue to import the alias.

## Internal retained-observation path

`_retained_native_inventory` is private but materially affects the public return. It only participates when authorization explicitly says `ATS_JOB_LINK_VERIFIED`; it rechecks collector/source binding and live guard state under a transaction (`services/company_discovery/ats_enrollment.py:139-163`). If the collector is already due, it returns `None`, causing a fresh collection. If not due, it returns a deliberately bounded `DEFERRED` result. Only a tightly matched, fresh, healthy discovery receipt upgrades that result to `RETAINED_OBSERVATIONS`; the comments explicitly say that successful operation/accepted counts do not establish complete provider inventory (`services/company_discovery/ats_enrollment.py:164-218`).

## Safe usage examples

### Pure tenant extraction from synthetic markup

```python
from services.company_discovery.ats_enrollment import greenhouse_tenant

tenant = greenhouse_tenant(
    "https://employer.example/careers",
    '<a href="https://job-boards.greenhouse.io/example">Open jobs</a>',
)
assert tenant == "example"
```

This exercises markup classification only. Do not substitute untrusted script text or infer a tenant from a company name.

### Enrollment from the bounded discovery boundary

```python
# Illustrative call shape only: keep provider I/O mocked/local in tests.
result = enroll_public_ats(
    store,
    candidate,
    resolved,
    career_url,
    fetched_career_html,
    profile_id=profile_id,
    profile_version=profile_version,
    check=deadline_and_cancellation_check,
    enrollment_guard=live_authorization_guard,
    provider="greenhouse",
)
```

Call this only after the bounded public discovery flow has fetched the career page and established employer authority. The production call site does exactly that for detected Greenhouse/Lever matches (`services/company_discovery/flywheel.py:499-507`). Because the function persists rows and can invoke collection, examples must use an isolated test store, fixture transport/provider adapter, and a real cancellation/deadline callback—not a production employer or real application endpoint.

## Tested contracts visible in the source tree

These are references to existing tests, not tests run while preparing this guide:

- Verified enrollment binds company, source, collector, and jobs; reopening the store preserves recurrence (`tests/integration/test_greenhouse_autodiscovery.py:75-113`).
- Disabled sources, unverified/ambiguous authority, preserved collector policy, revocation, and foreign ownership fail closed without unauthorized import (`tests/integration/test_greenhouse_autodiscovery.py:123-207`).
- Reviewed/user-confirmed enrollment persists its authorization and recurring binding without fabricating a company-domain row (`tests/integration/test_reviewed_greenhouse_enrollment.py:89-115`).
- Revoked/changed authority blocks egress and import, while disablement remains effective (`tests/integration/test_reviewed_greenhouse_enrollment.py:125-199,201-258`).

## Existing-documentation reconciliation

- `services/company_discovery/AREA.md:20-24` already states the verified-enrollment behavior, recurrence reuse, preservation of policy/disablement, and fail-closed boundaries. **no_new_gap:** do not duplicate that architectural narrative.
- `docs/ARCHITECTURE.md:149-164` already places ATS enrollment between public company resolution and recurring monitoring. **no_new_gap:** do not add another architecture overview.
- The useful gap addressed by this private draft is maintainer-facing symbol detail: the compatibility alias, call signatures, transaction ownership, exact return shapes, and error behavior.

## Maintenance cautions

- Do not treat an ATS tenant, provider display name, candidate name, or raw source observation as employer authority.
- Keep `check()` and the live `enrollment_guard` at their existing boundaries; caller-supplied confirmation is not sufficient by itself.
- Preserve lock ordering and the re-read comparison when changing binding logic.
- Do not silently re-parent existing tenant inventory, enable a disabled source, or overwrite collector policy during rediscovery.
- Do not describe retained observations as complete/current inventory; the source intentionally reports bounded observation scope.
- External application submission is outside this module and remains off in the documented area boundary.
