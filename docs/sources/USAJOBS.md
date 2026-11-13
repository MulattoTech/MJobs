# USAJOBS recent federal IT source — MJ-REQ-017.F06

## Scope and access

The `usajobs` provider is a bounded federal IT source. It uses USAJOBS's documented public Historic JOA metadata and Announcement Text endpoints. Their official documentation explicitly permits unauthenticated access; the authenticated Search API is outside this integration.

A collection requests occupational series **2210** over **seven inclusive UTC calendar dates**, ending on the collection date. It selects at most **25** announcements from the returned metadata page, ordered by opening date and then numeric control number, newest first. This does not cover every federal job, every agency, state or local government jobs, or announcements opened before that window.

Official sources, checked 2026-11-13:

- [Historic JOA metadata](https://developer.usajobs.gov/api-reference/get-api-historicjoa)
- [Announcement Text](https://developer.usajobs.gov/api-reference/get-api-announcementtext)
- [USAJOBS API terms and public-data provisions](https://developer.usajobs.gov/apirequest/index)

The public-data provisions allow reuse of public announcement data and encourage source identification, a link to the official announcement, retrieval dates, and disclosure of an unofficial service. MJobs is not operated, sponsored, or endorsed by OPM. Follow the official announcement's application instructions.

## Enrollment and exact capabilities

Choose **USAJOBS (recent federal IT)** in Sources. Its only tenant is `default`; employer tokens, agency credentials and USAJOBS account credentials are not requested. Enrollment alone performs no collection.

| Contract | Support |
|---|---|
| Collection strategy | PUBLIC_JSON |
| Discovery | Yes, bounded recent federal IT subset |
| Manual application redirect | Constructed official USAJOBS control-number link |
| Standalone detail fetch | Unsupported |
| Direct application submission | Unsupported |
| Application-status lookup | Unsupported |
| Salary normalization | Unsupported; unaltered source claims remain evidence |
| Fully remote / remote-eligibility metadata | Unsupported; telework is not inferred to mean fully remote |
| Complete inventory | Always false |
| Absence-based closure | Unsupported |

The API declaration contains exactly `DISCOVERY` and `APPLICATION_REDIRECT`. The collector remains responsible for policy admission, health, snapshots, provenance, cancellation and persistence. Application routes retain manual handling; a source observation cannot establish employer-domain or ATS authority.

## Request boundaries

A search makes at most two fixed-origin GET requests:

1. `https://data.usajobs.gov/api/historicjoa`, with series and opening-date bounds.
2. `https://data.usajobs.gov/api/historicjoa/announcementtext`, with only the selected control numbers.

Empty eligible metadata returns an empty, incomplete batch without a text request. No pagination, announcement-page crawl, external link fetch or Search API fallback occurs. Provider paging fields are never treated as proof of completeness.

Each response is bounded to 5,000,000 bytes. Metadata is limited to 1,000 records; text is limited to 25. The existing verified-TLS SourceTransport enforces cancellation and bounded reads. HTTP proxies from the process environment and redirects are disabled. Request timeout, rate and concurrency remain within the configured source policy. Rate limits and failures return explicit source errors; there is no internal retry loop.

The plural control-number query is a separate live qualification gate until a production-adapter read verifies the exact request and all selected joins.

## Identity, joining and raw evidence

Selection validates every metadata row's positive numeric control number, opening date, bounded collection structure, occupational-series values and raw fingerprint before deduplication. Conflicting records for the same control number fail the search, including records outside the final selection.

Full agency, department and announcement identity is required for every row in the deterministic newest-25 selection, before any Announcement Text request. An unsupported agency label on an older, unselected row does not assign a company or block the selected subset. If an unsupported identity falls inside the selected subset, the whole search fails; the adapter does not substitute a display name, invent an agency code or fill its slot from older records. The batch remains an explicitly incomplete selection, not a count of all valid or rejected metadata records.

Each posting has a provider-native numeric control number. Metadata and text must agree on:

- Control number.
- Agency code.
- Department code.
- Opening calendar date.
- Announcement number.

Missing, extra, mismatched or conflicting text records fail the entire search before domain persistence. Identical duplicate records deduplicate; conflicting duplicates fail. Documented camel-case and observed lowercase collection aliases are accepted only when they agree if both occur.

Company identity is the tuple `("usajobs", department_code, agency_code)`. The stable display label is `USAJOBS agency <code>`; nullable or changed upstream names remain separate source evidence and cannot create false identity conflicts. The common USAJOBS host is not a verified employer website.

Every source item retains the complete joined envelope:

```json
{
  "metadata": {"provider fields": "unaltered"},
  "announcement_text": {"provider fields": "unaltered"}
}
```

The normal collector stores this envelope in immutable snapshots and binds normalized values to the exact USAJobs parser implementation. Repeat observations retain stable posting identity, while changed content appends history.

## Dates, status and normalization limits

Opening, closing and expiry fields are calendar-date claims without an established timezone. MJobs preserves them separately with precision metadata. It does not invent midnight-UTC posting instants; `reported_posted_at` and `reported_posted_at_raw` remain null.

Only explicit structured opening-status claims can affect lifecycle normalization. Accepting-applications status means possibly active; a null or unfamiliar status remains unknown. A close date, expiry date, missing record or empty rolling window cannot establish that a position is filled, removed, expired or open for applications. Generic closed-to-applications status does not invent a filled reason.

Announcement sections are converted to bounded plain text through the existing structured-text sanitizer. Requirement and skill extraction use that text. The raw metadata and text remain available as evidence. Telework flags and salary numbers are preserved as source claims; this integration does not establish salary currency or unrestricted remote eligibility.

The listing link is constructed as `https://www.usajobs.gov/job/<control-number>`. Normalized output identifies that construction explicitly and does not claim that an application route or employer submission path was verified. Follow the current official page manually.

## Verification and last tested

The first read-only schema probe ran on **2026-11-13 03:46 UTC**:

- One metadata request for series2210, opening dates November7–13, returned117 rows.
- One selected control-number text request returned one matching text record.
- Both returned a data array; neither supplied paging metadata in that observation.
- Collection names were lowercase in the response, some agency display names were null, and a future close date occurred with unknown opening status.

This probe is schema evidence, not qualification of the completed adapter or its plural query. Synthetic fixtures are explicitly fictional and are not substituted for live proof.

Completed-adapter tests, actual browser enrollment, bounded live batch verification, immutable review and hosted checks are recorded in `docs/evidence/2026-11-13-usajobs-source.json` when executed. Until that evidence exists, those checks remain **NOT_RUN**. Protected integration, deployment and canonical acceptance are separate gates.

The browser case exercises the actual React/API enrollment, exact capabilities, visible scope after reload, disablement, and zero queued operations/applications/provider requests in an isolated fixture workspace.
