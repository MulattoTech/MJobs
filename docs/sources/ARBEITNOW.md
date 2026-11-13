# Arbeitnow public niche-source adapter

Requirement: `MJ-REQ-017.F07` · Parent: `MJ-REQ-017` · Provider ID: `arbeitnow`

This provider reads the latest page of Arbeitnow's public European job feed. It uses the normal MJobs source configuration, collection, health, immutable posting evidence and provenance flow. It is an opt-in source: adding the provider to the software does not enroll it or enable background collection.

The November 12, 2026 parallel execution instruction explicitly activates this P6 delivery. The original requirement ID, priority and acceptance criteria are unchanged. Implementation, current test evidence, protected integration, deployment and formal acceptance remain separate.

## Scope and capabilities

| Capability | Support |
| --- | --- |
| Collection strategy | Public JSON, one bounded request to the fixed feed endpoint |
| Discovery | Latest public feed page; no exhaustive inventory claim |
| Remote metadata | Provider's explicit remote flag and supplied location text |
| Application redirect | Validated, unchanged Arbeitnow listing URLs on the documented regional sites |
| Job detail API | Unsupported; `fetch()` fails without making a request |
| Structured salary | Unsupported |
| Direct application submission | Unsupported |
| Application status | Unsupported |
| Closure or expiration detection | Unsupported; absence from this partial page does not close existing jobs |

The feed primarily covers Europe. A remote flag does not establish eligibility to work from the United States, worldwide availability, residence, citizenship, visa status, or a particular timezone. MJobs retains the supplied location and does not infer those facts from the source's regional domain.

The adapter reads one latest page only and returns `complete_inventory=False`. It does not follow provider-supplied pagination links, crawl job pages or claim to retrieve every open role. The provider can change the page size. Response-byte and item limits are enforced by the adapter, with malformed or oversized responses rejected instead of silently importing a truncated inventory.

## Setup and user-visible source credit

Choose **Arbeitnow (Europe)** in Public job sources once the matching frontend change is integrated. The only supported tenant is `default`; no account, password, API key or browser session is required.

A source can also be configured through the existing authenticated/local MJobs API:

```json
{
  "provider": "arbeitnow",
  "tenant": "default",
  "enabled": true,
  "poll_interval": 3600,
  "request_timeout": 10,
  "rate_limit": 6,
  "concurrency": 1,
  "jitter_seconds": 60
}
```

Send that document to `PUT /sources` only when choosing to enable this source. Existing source cooldown, retry/backoff, saved-search scheduling and operator disablement apply. The hourly interval is a conservative configuration example for a latest-page feed, not a guarantee about upstream update timing.

The product must display a visible **Jobs supplied by Arbeitnow** link to [Arbeitnow](https://www.arbeitnow.com/), as required by its API terms. The source selector and source panel provide the corresponding source context. Documentation alone does not satisfy that product requirement; the evidence manifest records frontend integration and browser verification separately.

## Source identity and safe links

All network requests go to:

```text
https://www.arbeitnow.com/api/job-board-api
```

Transport uses the existing source request budget, cancellation checks and bounded response handling. Certificate verification stays enabled, environment proxy inheritance is disabled, and redirects are not followed. Neither feed pagination URLs nor job links determine an outbound collection destination.

Listing links are accepted only on the explicitly supported Arbeitnow regional HTTPS hosts:

- `www.arbeitnow.com`
- `www.arbeitnow.co.uk`
- `www.arbeitnow.fr`
- `www.arbeitnow.ch`
- `www.arbeitnow.nl`

The accepted job path binds the company slug and exact native job slug. User information, unexpected ports, query/fragment components, malformed or ambiguous paths, and other origins are rejected. An adapter does not discover a new allowed origin from an arbitrary response.

A company's source identity includes the provider, validated regional host and native company slug. Its display name is retained separately. Same-looking names do not merge employers across sources or regions, and an upstream identity conflict cannot overwrite an existing company's identity. Arbeitnow domains are classified as aggregator domains and cannot become verified employer domains through discovery.

The normal collector stores the unmodified public posting and immutable snapshots. Normalized parser provenance identifies `services.discovery.sources.arbeitnow.Arbeitnow.normalize`. Routes remain aggregator evidence: this provider is not added to the trusted integrated-board or first-party ATS authority lists. Real external application submission remains off under the existing application controls.

## Failure and recovery behavior

- Disabled sources fail before egress through the normal collector.
- Rate limits retain bounded provider retry information and trigger the existing persisted cooldown/backoff path.
- Network, content-type, JSON and required-field failures retain stable source error classifications.
- A local cancellation or expired operation budget preserves its original outcome and does not poison provider health.
- Repeated collection uses stable source, company, posting and job identities.
- The incomplete page cannot mark unseen jobs closed.
- Unsupported details and salary data are not fabricated.

Deterministic tests use synthetic payloads and `httpx.MockTransport`; they never contact a provider. Live validation is explicitly invoked, read-only and recorded separately. There is no CAPTCHA, authentication or anti-bot workaround.

## Verification and evidence

Run the provider and collector boundary from the repository root:

```text
python -m pytest -q tests/unit/test_arbeitnow_source.py tests/unit/test_arbeitnow_provenance.py tests/integration/test_arbeitnow.py
python -m pytest -q tests/unit/test_company_source_capabilities.py tests/unit/test_company_source_contract_sustain_224.py tests/unit/test_extension_point_contracts.py tests/unit/test_source_adapter_contract_223.py
git diff --check
```

The dated evidence manifest is `docs/evidence/2026-11-13-arbeitnow-source.json`. It records the exact tested revision, executed commands, results, live-validation receipt, review, frontend status and remaining gates. An unexecuted command, mock response, registry entry or passing narrow test does not establish formal requirement acceptance.

## Provider references

Official references inspected for this implementation:

- [Public API announcement and documentation link](https://www.arbeitnow.com/blog/job-board-api).
- [API documentation](https://documenter.getpostman.com/view/18545278/UVJbJdKh).
- [Public JSON feed](https://www.arbeitnow.com/api/job-board-api).
- [Official homepage and regional site links](https://www.arbeitnow.com/).
- [Terms, section 11: API and platform link-back requirement](https://www.arbeitnow.com/terms).

Access is subject to the provider's current terms and availability. Last-tested facts belong in the evidence manifest rather than being inferred from this guide.
