# Company Discovery Area

Owns company identity resolution, trigger policy, official career-source discovery, automatic source enrollment, and immediate employer enumeration.

## Boundaries
- API/scheduler may enqueue discovery but do not perform external discovery I/O.
- External company discovery executes only on the discovery worker.
- Company identity decisions must return confidence and evidence.
- Low-confidence/unsupported infrastructure must surface review/block state rather than guess.
- Provider-specific ATS detection/collection remains in `services/ats_detection` / source adapters as those requirements activate.
- Never bypass access controls, anti-bot protections, CAPTCHA, or authentication.
- Preserve all source/provenance history when employer infrastructure changes.

## Current slice
MJ-PR-016 establishes the durable trigger/resolver/flywheel contract and proves it against the explicit local fixture. Production official-domain/career crawling and additional ATS detectors are separate tracked requirements.

## Companies/Sources read model and controls
`list_company_source_view` projects persisted source-specific feed postings and current-profile match records without treating candidate domains as official or inferring missing scheduling. Explicit pin/enabled/watch choices remain independent. New discovery events carry `company_source_id`; unlinked historical events remain unavailable. `apps/web/src/company-sources.tsx` renders this contract with unsupported actions clearly disabled.

## Verified Greenhouse enrollment

Public discovery can enroll an unambiguous Greenhouse board linked or embedded on an independently verified official career page. It reuses the existing bounded read-only adapter, collector policy, posting snapshots, canonical deduplication, matching and saved-search scheduler. The persisted source binds collection to the original employer ID and is reauthorized before fetching and inside the import transaction. Source-host locks serialize competing owner claims; conflicting historical owners require review rather than re-parenting data.

Automatic enrollment preserves existing collector policy, source disablement and explicit saved-search source scopes. Disabling either source, revoking official-domain evidence, or introducing conflicting ownership prevents import and recurrence. A provider failure leaves the enrolled source available for bounded recovery; local authority revocation does not falsify provider failure history. Companies/Sources shows linked posting counts and the actual collector state without inventing a next-check timestamp. Unverified domains, ambiguous tenants and other ATS providers retain explicit review/unsupported boundaries. External submissions remain OFF.


## Source-quality observations

MJ-REQ-083 remains PARTIAL. `quality.py` stores source-attributed observations and exposes bounded API scores from `quality_score.py`. Configured collectors explicitly link company-source rows; their statistics retain board-wide scope. Feed refresh failures are attributed only to enrolled exact portals. Seven equal-weight components retain unknown values and coverage; collection freshness is not job age and historical uptime means observed attempt availability. Neither quality nor collection success verifies employer identity or authorizes submission. No history is fabricated on migration. Populated downgrade refuses evidence loss. Bounded public-feed link observations are now available; other provider scopes remain follow-up work. Quality evidence is scoped to the observed endpoint; legacy observations without an endpoint remain unknown for endpoint-specific comparisons. Authority-first duplicate selection is owned by services/dedupe/source_values.py.

## Public company discovery

PERSONAL user requests explicitly select PUBLIC_HTTPS operations. Legacy fixture operations cannot cross workspace modes. `public_client.py` owns bounded resolver work, numeric socket pinning, certificate/hostname verification, exact-origin redirects, robots checks, rate/request/byte/deadline limits and cancellation. DNS future waits poll the caller's cancellation/deadline check while retaining the absolute DNS timeout; active HTTPS requests also poll that check and close their request sockets promptly, preserving its error. No credentials, cookies, proxy or submission path is introduced. `public_resolution.py` uses qualifying stored domain evidence for existing identity; other sites receive isolated candidate IDs, without name-only or claimed-tenant binding. New generic feeds remain review-required and disabled unless identity is already verified; later explicit enable preferences remain independent and preserved. Generic feeds do not create ATS tenant records. Supported feeds can populate jobs during a one-time request. ATS-provider breadth and full source link acceptance remain open; supported reviewed feeds now have recurring monitoring.

## Durable useful-job triggers

Direct PERSONAL Add Company requests share one queued/running operation only for the same candidate, currently resolved company, profile and immutable profile version. Admission and each explicit request's receipt commit together in the existing outbox/audit ledger. Replaying either of two coalesced requests retains the original operation after failure; a new request can start a fresh terminal-failure recovery. Different profiles, candidate URLs/provenance and review/monitoring authority remain separate. Company discovery remains visible in the requesting profile's Activity and dead-letter history after reload. This covers direct Add admission, not all task-idempotency acceptance.

PERSONAL configured-source intake records unseen-company and strong-match requests in the same transaction as jobs, matches and the durable outbox. Rescoring also records strong matches. Requests coalesce per company/source; matching writes precede globally ordered request locks. A changed selected candidate cannot silently change that order. Successful verified same-origin feeds do not recursively rediscover themselves.

Without independent employer-domain evidence, the request waits for website review in Sources. The user can dismiss it or explicitly request a bounded public read. Review does not verify employer identity; unknown sites remain separate candidate companies. Shared ATS/aggregator URLs are not employer origins. Qualified unambiguous stored domain proof permits automatic queueing. Every worker fetch boundary rechecks request revision, exact source/candidate binding, source enablement and domain authorization. Enrollment revalidates identity under the same short transaction as the source write, without holding locks across network requests.

No failed operation is automatically reopened. Explicit review can retry terminal failures with a new revision. Fresh intake after a changed source can renew a non-running request while auditing previous authorization and preserving the old operation. Dismissals remain dismissed. Migration fabricates no historical requests and refuses a populated downgrade.

MJ-REQ-054/056/060 remain PARTIAL: configured intake, rescoring, explicit preferred-company decisions and supported broken-feed triggers are wired; saved companies and repeated aggregator appearances remain open. A preferred decision from job detail binds the selected posting to the employer, commits preference and discovery intent together, and reuses coalesced requests, policy caps, verified-origin admission and website review. Replays never create another operation; manual/source-less jobs preserve the preference with an explicit nonqueued outcome. Automatic official-domain acquisition is not implemented, so typical shared-board jobs require review. Supported company-feed scheduling revalidates authorization before egress and import. Source monitoring indicators do not certify current employer identity.

## Reviewed recurring public-feed monitoring

`monitoring.py` owns exact-source policy revisions, bounded scheduling and manual refresh, failure backoff, and durable import receipts for same-origin RSS/Atom and JSON feeds. Independently verified domain sources can acquire policy automatically; candidate origins require explicit source-version/revision review. Legacy enabled flags create no authority. Automatic refresh also requires an enabled unrestricted search profile; manual refresh selects one existing profile. The scheduler writes an outbox operation only; all public reads use the discovery worker and existing bounded robots-aware client.

Each request rechecks source, domain proof, policy revision, operation attempt/lease and cancellation. The import transaction locks domain evidence, source, monitoring policy, operation and selected search-profile rows in that order; every selected automatic profile must still authorize unrestricted reads. Jobs, profile matches, observations, source success and the completed-operation receipt commit together. The receipt prevents refetch/reimport after an import-commit/worker-acknowledgment crash. Local authorization rejection is not an employer feed-quality failure.

Structural career/feed changes queue bounded rediscovery while preserving the existing source and history. A different discovered endpoint produces an audited migration-review error; this slice does not implement provider replacement. Migration backfills no monitoring grants and refuses a populated downgrade. Sources exposes review, interval, run-now, status, last attempt and effective next check. Full ATS breadth and MJ-REQ-077 migration acceptance remain open.

## Sampled application-link observations

PUBLIC_HTTPS feed refreshes use the same robots-aware, origin-bound client for up to five distinct accepted posting identities. A UTC-day/hash ordering rotates samples without new clients, retry or submission. Exactly one bounded JSON-LD JobPosting must agree with the imported external identifier, title and full requested/final URL before declaring a match. HTTP 404/410 are observed negative link results; blocked, missing, conflicting and unsupported identity remains UNKNOWN. This proves declared identity only, not employer authority, a usable application form or current job availability.

Reports are revalidated against the raw SHA-256, exact URL, endpoint and time before insertion beside their immutable candidate and source-quality batch. Successful import, source last-success and observations commit together; failures retain their own collection observation. No backfill or route replacement is performed. The API/UI shows sampled/conclusive/unknown counts and accepted feed rows (which can repeat identities). Quality uses only conclusive checks; UNKNOWN is never zero accuracy. Other ATS origins need independently authorized provider scope; periodic collection is supported for reviewed same-origin feeds, while full MJ-REQ-083 acceptance remains open.
