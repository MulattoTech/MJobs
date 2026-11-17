# MJ-REQ-292 — proposed employer-controlled board proof (V3)

Status: **DESIGN ONLY / NOT AUTHORIZED FOR RUNTIME USE**. Issue [#293](https://github.com/MulattoTech/MJobs/issues/293). Baseline: `f4fd16e3a61e2b44e2985ff3fe441d22a9c32941` (2026-11-17 UTC).

## Problem and non-goals

The connected 60-step campaign observed 53 PASS and 7 PARTIAL (25, 39–43, 50); the formal ledger remains 0 PASS / 53 PARTIAL / 7 NOT_RUN. The frozen 100-item WWR cohort has no source-description same-job Greenhouse link. A bounded Remotive response of 17 records also contained none. Some WWR Apply links lead to account registration; an account wall is not employer authority.

Current `GREENHOUSE_RECIPROCAL_V1` explicitly binds `origin_scope=PERSISTED_HINT_AND_RECIPROCAL_EMPLOYER`. `AGGREGATOR_NATIVE_BOOTSTRAP_V2` validates one specific Greenhouse job ID/title and one employer-controlled job page. `greenhouse_board_tenants` can already parse a public board URL on an employer page, but board discovery is **not** currently a permitted bootstrap grant.

Do not change V1/V2 semantics, fabricate native links, infer ownership from the employer name, bypass account/robots/CAPTCHA controls, contact employers, submit real applications, or promote any acceptance step from this design.

## Proposed V3 trust sequence

1. **New explicit consent:** introduce a distinct preference mode such as `EMPLOYER_BOARD_RECIPROCAL_V1` only through a reviewed schema migration and a newly minted activation ID. Existing opted-in V1 users remain V1; default is disabled. Persist profile version, consent/activation and revocation fences.
2. **Untrusted aggregator lead:** admit only a durable raw posting snapshot and source/profile binding, with its exact job title, source URL, company label and stable candidate ID. No authorization or employer-domain assertion is accepted from the aggregator record.
3. **Public employer origin acquisition:** require a deterministically selected public employer-controlled URL *reachable through a validated source link* and revalidate it independently. A generic company name, WWR account URL, aggregator-hosted company profile, guessed DNS, search snippet or arbitrary user-provided domain cannot bootstrap employer authority. If no qualifying link exists, stop `COMPANY_DOMAIN_CORROBORATION_REQUIRED`.
4. **Board evidence on employer origin:** fetch the employer page with the existing protected HTTP/robots/SSRF/rate policy and parse exactly one supported Greenhouse board tenant via `greenhouse_board_tenants`. A board URL from the aggregator alone is not reciprocal proof. Reject zero or multiple tenants and cross-origin redirects.
5. **Bounded native inventory:** fetch that tenant's official public Greenhouse API inventory under the same finite item budget. Filter candidate jobs using strict normalized title and an independently evidenced requisition/job identity when present. Reject zero matches, multiple matches, deleted or stale jobs and any mismatch between board/API/aggregate record.
6. **Reciprocal job proof:** for the sole matched job, obtain its employer-controlled `absolute_url` and verify canonical JobPosting/title/company plus the exact native ID in the employer job/apply document using existing `corroborated_job_page` rules. A board API title match alone is insufficient.
7. **Durable receipt and write fence:** record bounded source, board, API and employer document hashes; origin-role ledger; exact candidate/raw/profile/source version/activation; selected tenant and job ID; parser and policy versions. Recalculate and lock all persisted bindings immediately before any company-domain and ATS-enrollment mutation. Preserve disable/revocation and concurrency checks.
8. **Native continuation:** only after verified enrollment may normal employer-only job discovery, duplicate reconciliation and fictional mock-ATS acceptance proceed. Never treat board enumeration as an application.

## Budget and versioning constraints

The existing V2 proof permits at most four documents, ten HTTP requests, three origins and 30 seconds. A V3 board-inventory step may require a different **reviewed finite budget**, not silent reuse of the V2 receipt shape. Keep independent roles `SOURCE_LISTING`, `EMPLOYER_BOARD`, `NATIVE_BOARD_API`, `EMPLOYER_JOB`; allow only explicitly authorized origins. Bound inventory size and total response bytes; never follow arbitrary URLs from a job description. Preserve non-retry terminal failure on access walls and infeasible rate windows.

Implementation spans `packages/domain/employer_verification.py`, `services/company_discovery/aggregator_bootstrap.py`, `services/company_discovery/aggregator_workflow.py`, `services/company_discovery/requests.py`, `services/company_discovery/ats_enrollment.py`, and their consent, DB, API and integration tests. This is a separate reviewed source change, not a one-line parser switch.

## Mandatory test matrix before implementation/activation

| Case | Controlled input | Required outcome |
|---|---|---|
| P1 valid reciprocal chain | Aggregator lead → employer-owned careers page → unique GH tenant → exact native job → same-origin JobPosting/apply | V3 receipt only, after current persisted authorization and all caps |
| N1 company-name spoof | Aggregator says Acme but link is unrelated employer | Reject; zero company-domain/ATS writes |
| N2 aggregator-only board | Greenhouse tenant appears only on an aggregator company profile | Reject; employer-controlled source not proven |
| N3 gated Apply | Apply link routes to WWR registration or 403 | Stop without login bypass or employer egress |
| N4 cross-job link | Bloomreach Staff DevOps description links Senior DevOps requisition | Reject; never merge different positions |
| N5 duplicate native titles | One board contains two jobs with same normalized title | Reject ambiguous; never choose first or newest by guess |
| N6 board mismatch | Employer page names two boards, or API returned tenant differs | Reject; no silent fallthrough |
| N7 employer site mismatch | Native API `absolute_url` points off proven employer origin | Reject |
| N8 uncorroborated listing | Native API title matches but employer JobPosting ID/title/apply page does not | Reject |
| N9 stale snapshot | Raw posting/profile/source/activation changes between fetch and write | Reject and preserve original rows |
| N10 revocation race | Profile disables consent while job API fetch is in progress | Cancel; zero writes |
| N11 rate/bytes/robots | Slow, oversized, redirect, DNS-to-private, robots-denied or 503 route | Bounded terminal failure, no retry storm |
| N12 V1/V2 replay | Existing V1/V2 receipt presented as V3 or vice versa | Reject schema/consent mix; no migration by inference |
| N13 unrelated source | Lever, unsolicited website or unauthenticated tenant passed as Greenhouse | Reject |
| N14 duplicate reconciliation | Same employer but different native job ID/title | Keep distinct; no false dedupe |

Test layers: pure policy/schema validation; controlled transport positive/negative; PostgreSQL transaction and revocation races; exact-head disposable Compose integration; native mock ATS only for fictional submission; independent review and hosted CI. Evidence must include negative and positive receipts, complete sanitized request/origin budget, source/image hashes, and before/after domain-source row counts.

## Decision gate and checkpoint

**Decision: do not implement V3 in the current #293 campaign without source-policy review.** The available WWR and Remotive cohorts cannot supply a proven employer-controlled starting origin through the current public path; V3 would still stop at step 3, so it is not an evidence-backed shortcut to 60/60. First find an eligible source with a public employer-controlled link, then review the V3 consent and receipt contract, implement a small independently testable slice, and integrate through protected PR/CI before a new whole-journey run.

The last saved safety regression is `1eda97c18d779f0837d5b6025cc7ad3b51b7412d` on `web/mj293-foreign-native-title-20261117` (66 tests passed, 16 skipped, 32 subtests passed); it is not merged. This document also is not a formal acceptance artifact and must not alter `planning/acceptance/MJ-REQ-292.json`.
