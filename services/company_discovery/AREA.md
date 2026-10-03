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


## Source-quality observations

MJ-REQ-083 remains PARTIAL. `quality.py` stores source-attributed observations and exposes bounded API scores from `quality_score.py`. Configured collectors explicitly link company-source rows; their statistics retain board-wide scope. Feed refresh failures are attributed only to enrolled exact portals. Seven equal-weight components retain unknown values and coverage; collection freshness is not job age and historical uptime means observed attempt availability. Neither quality nor collection success verifies employer identity or authorizes submission. No history is fabricated on migration. Populated downgrade refuses evidence loss. Bounded public-feed link observations are now available; other provider scopes remain follow-up work. Quality evidence is scoped to the observed endpoint; legacy observations without an endpoint remain unknown for endpoint-specific comparisons. Authority-first duplicate selection is owned by services/dedupe/source_values.py.

## Public company discovery

PERSONAL user requests explicitly select PUBLIC_HTTPS operations. Legacy fixture operations cannot cross workspace modes. `public_client.py` owns bounded resolver work, numeric socket pinning, certificate/hostname verification, exact-origin redirects, robots checks, rate/request/byte/deadline limits and cancellation. No credentials, cookies, proxy or submission path is introduced. `public_resolution.py` uses qualifying stored domain evidence for existing identity; other sites receive isolated candidate IDs, without name-only or claimed-tenant binding. New generic feeds remain review-required and disabled unless identity is already verified; later explicit enable preferences remain independent and preserved. Generic feeds do not create ATS tenant records. Supported feeds can populate jobs during a one-time request. ATS-provider breadth, scheduling and full source link acceptance remain open.

## Durable useful-job triggers

PERSONAL configured-source intake records unseen-company and strong-match requests in the same transaction as jobs, matches and the durable outbox. Rescoring also records strong matches. Requests coalesce per company/source; matching writes precede globally ordered request locks. A changed selected candidate cannot silently change that order. Successful verified same-origin feeds do not recursively rediscover themselves.

Without independent employer-domain evidence, the request waits for website review in Sources. The user can dismiss it or explicitly request a bounded public read. Review does not verify employer identity; unknown sites remain separate candidate companies. Shared ATS/aggregator URLs are not employer origins. Qualified unambiguous stored domain proof permits automatic queueing. Every worker fetch boundary rechecks request revision, exact source/candidate binding, source enablement and domain authorization. Enrollment revalidates identity under the same short transaction as the source write, without holding locks across network requests.

No failed operation is automatically reopened. Explicit review can retry terminal failures with a new revision. Fresh intake after a changed source can renew a non-running request while auditing previous authorization and preserving the old operation. Dismissals remain dismissed. Migration fabricates no historical requests and refuses a populated downgrade.

MJ-REQ-054/056/060 remain PARTIAL: only configured intake and rescoring are wired; saved/preferred companies, repeated aggregator appearances and broken-source triggers remain open. Automatic official-domain acquisition is not implemented, so typical shared-board jobs require review. Company-feed scheduling is not implemented; before adding it, automatically enabled sources must revalidate domain proof before egress, rather than treating a historical enabled flag as enduring authority. Source monitoring indicators do not certify current employer identity.

## Sampled application-link observations

PUBLIC_HTTPS feed refreshes use the same robots-aware, origin-bound client for up to five distinct accepted posting identities. A UTC-day/hash ordering rotates samples without new clients, retry or submission. Exactly one bounded JSON-LD JobPosting must agree with the imported external identifier, title and full requested/final URL before declaring a match. HTTP 404/410 are observed negative link results; blocked, missing, conflicting and unsupported identity remains UNKNOWN. This proves declared identity only, not employer authority, a usable application form or current job availability.

Reports are revalidated against the raw SHA-256, exact URL, endpoint and time before insertion beside their immutable candidate and source-quality batch. Successful import, source last-success and observations commit together; failures retain their own collection observation. No backfill or route replacement is performed. The API/UI shows sampled/conclusive/unknown counts and accepted feed rows (which can repeat identities). Quality uses only conclusive checks; UNKNOWN is never zero accuracy. Other ATS origins need independently authorized provider scope; periodic collection and full MJ-REQ-083 acceptance remain open.
