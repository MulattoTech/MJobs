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
2. `https://data.usajobs.gov/api/historicjoa/announcementtext`, with the same fixed series and opening-date bounds.

Empty eligible metadata returns an empty, incomplete batch without a text request. No pagination, announcement-page crawl, external link fetch or Search API fallback occurs. Provider paging fields are never treated as proof of completeness.

Both requests use only `PositionSeries`, `StartPositionOpenDate` and `EndPositionOpenDate`; the date window is computed once per search. The Announcement Text reference documents these bulk filters. No control-number batch encoding is assumed.

Each response is bounded to 5,000,000 bytes and 1,000 records. At most 25 selected metadata records can become source items, regardless of the text response size. The existing verified-TLS SourceTransport enforces cancellation and bounded reads. HTTP proxies from the process environment and redirects are disabled. Request timeout, rate and concurrency remain within the configured source policy. Rate limits and failures return explicit source errors; there is no internal retry loop.

The fixed bulk series/date flow was observed successfully on **2026-11-13 05:46–05:47 UTC**, including both requests and all 25 selected joins. This is one bounded observation of a changing source, not evidence of complete inventory. A text response that exceeds the bounds or omits a selected control fails; neither pagination nor an alternative query repairs it.

## Identity, joining and raw evidence

Selection validates every metadata row's positive numeric control number, opening date, bounded collection structure, occupational-series values and raw fingerprint before deduplication. Conflicting records for the same control number fail the search, including records outside the final selection.

Full agency, department and announcement identity is required for every row in the deterministic newest-25 selection, before any Announcement Text request. An unsupported agency label on an older, unselected row does not assign a company or block the selected subset. If an unsupported identity falls inside the selected subset, the whole search fails; the adapter does not substitute a display name, invent an agency code or fill its slot from older records. The batch remains an explicitly incomplete selection, not a count of all valid or rejected metadata records.

Each posting has a provider-native numeric control number. Metadata and text must agree on:

- Control number.
- Agency code.
- Department code.
- Opening calendar date.
- Announcement number.

Every text row must have a valid positive numeric control number and opening date, bounded known collection structure and a strict raw fingerprint. The adapter checks duplicate controls across the entire bounded response: identical records deduplicate, while conflicting duplicates fail even when the control is unselected. Documented camel-case and observed lowercase collection aliases are accepted only when they agree if both occur. Text records do not have to repeat metadata's occupational-series collections; supplied `positionSeries` and other source fields remain raw evidence.

Additional unselected text records are expected from the bulk endpoint. They cannot create companies, source items or postings, and unsupported agency identity on those unselected rows is not used. Every selected text record still requires the full native identity above. Missing selected text, mismatched identities or conflicting records fail the entire search before domain persistence. Extra records never fill a missing selected slot.

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

This probe is schema evidence, not qualification of the completed adapter or the bulk series/date query. Synthetic fixtures are explicitly fictional and are not substituted for live proof.

A later production-adapter read on **2026-11-13 05:04–05:05 UTC** selected 25 metadata records but failed on the former comma-separated control-number text request. A separately bounded same-query diagnostic at **05:15 UTC** returned HTTP 400 with an integer-value parsing error. Those failure receipts remain preserved and remain distinct from the later replacement-flow qualification.

The replacement production-adapter flow ran once on **2026-11-13 05:46–05:47 UTC**, at source head e9594dd84868cc0828aca34da40cfb31d0029390. Both fixed API requests returned HTTP 200 for the same series 2210 and November 7–13 window. Each response contained 117 rows. All 25 selected native identities joined and normalized without errors; raw envelopes remained unchanged and source health was healthy. The normalized lifecycle claims were 12 possibly active and 13 unknown, without inferring status from dates.

Only after that successful batch, one constructed listing URL returned a bounded HTML HTTP 200 response from the exact official host, with verified TLS and no redirects. This establishes that one URL responded. Page identity, application instructions, submission and future routes remain unverified; normalized routes continue to identify their construction explicitly.

The latest integrated local candidate incorporates protected main `1c1884e8cf69bb189ebd6bd725ed0a42ece0aab8`. Its F06 backend and browser tests were executed on merge candidate `a1bfd71c25e0b7f0ee32f82b18e89242b0004d14` and remain bound to the reconciled candidate `e58d3d200391f18ce9c253cfecdf781a7acbf03d` (tree `f281993138f430f96be54a62069b12733eee0f08`) by exact equality of the relevant adapter, collector, registry, UI, fixture, and test blobs. The later Email-only main merge changed none of those F06 inputs; the checks below are inherited by input equivalence, not described as reruns at `e58d3d2`.

- The focused backend/source-contract command passed **262 tests and 59 subtests**, with **4 skips** and no failures, on `a1bfd71c`.
- The web production build passed on `a1bfd71c`.
- The web Vitest suite passed **1,261 tests across 164 files** on `a1bfd71c`.
- The current USAJOBS enrollment browser case passed **1 test** using installed local Chrome on `a1bfd71c`. It used isolated local fixtures and made no external application or live provider request.
- A separate earlier merged-frontend qualification at `7db8d97a0afd7cf711ee3176ff97a63c3b85e048` passed a 28-case Desktop/Activity unit subset and five mounted-browser cases. Those historical counts remain attached to that exact revision.

The bounded production-adapter observation remains dated **2026-11-13 05:46–05:47 UTC** at `e9594dd84868cc0828aca34da40cfb31d0029390`; it is not a fresh or current provider-success claim. The USAJOBS adapter and verified-TLS transport blobs at `e9594dd8` are identical to those in the latest candidate. The observation used at most two fixed-origin API GETs, one concurrency, 20-second per-request ceilings, 5,000,000-byte and 1,000-row response bounds, and at most 25 selected items. No new live request was made during the later main-merge verification.

The issue #316 scoped acceptance criteria are qualified in the candidate matrix, with the exact source/test bindings and dated live receipt recorded in `docs/evidence/2026-11-13-usajobs-source.json`. The USAJOBS listing route remains constructed and manual-only; page identity, employer ATS authority, complete inventory, OS-visible notifications, and any application submission are not established. Hosted checks on the updated PR head, protected integration, deployment, and parent `MJ-REQ-017` completion remain separate pending gates.

The browser enrollment case exercises the actual React/API flow, exact capabilities, visible scope after reload, disablement, and zero queued operations or applications in an isolated fixture workspace.
