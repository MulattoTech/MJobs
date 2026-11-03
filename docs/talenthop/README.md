# TalentHop capability inventory and MJobs crosswalk

Issue/epic: [#904](https://github.com/MulattoTech/MJobs/issues/904). Reconnaissance: [#903](https://github.com/MulattoTech/MJobs/issues/903). Fleet: [#905](https://github.com/MulattoTech/MJobs/issues/905).

This is an **incomplete, public-evidence checkpoint**, not a completed site audit or accepted parity. The machine-readable source is [catalog.json](catalog.json). It is additive to the original and reference acceptance matrices and never changes their denominators.

## Coverage and access

At this checkpoint: **3 route patterns identified, 0 interactively exercised; 12 capability/journey groups identified, 0 exercised; 1 public homepage source retrieved.** The browser exposed an existing dashboard tab's route, then automatic approval review denied page observation because direct user authorization for private candidate data was absent. No page content, credentials, resume, candidate record, or session token was read. No blocked observation was retried through another surface.

Public source: [TalentHop homepage](https://www.talenthop.com/), retrieved November 3, 2026 UTC. Eight feature groups are PUBLICLY CLAIMED, two INACCESSIBLE / AUTHORIZATION REQUIRED, and two NOT FOUND / UNKNOWN; zero OBSERVED. Unknown does not mean absent. The third route pattern comes from the historical supplied-link example in [#517](https://github.com/MulattoTech/MJobs/issues/517), not current navigation.

HAR status: **BLOCKED**. The connected Browser exposes DOM, screenshot, console and page-asset capabilities, not full request/response HAR export. Private dashboard observation is independently blocked. No full HAR, partial network trace, raw archive, sanitized captured derivative, byte size or capture hash exists. The offline sanitizer is implemented and tested separately; its existence does not establish a capture. No approved private encrypted raw-artifact destination was established.

Desktop/mobile UI, keyboard behavior, accessibility, error/offline states, network timings, pagination, subscriptions, auth choices and destructive settings were **NOT_RUN**. Robots/sitemap/linked-page breadth, technology/network architecture, hidden roles and paid tiers remain unknown. Search retrieval is not a substitute for exercising the real UI.

## Initial crosswalk

| Feature IDs | Qualified discovery lead | Existing MJobs basis / remaining work |
| --- | --- | --- |
| 001–003 | Remote discovery, listing trust, preferences | Search semantics, source-candidate provenance and preference scoring exist. Validate actual facets, authenticity evidence and persistence; reuse #26/#517/#192. No claim that every listing is verified. |
| 004–005 | Resume enhancement and ATS alignment | Bounded imports, reviewed AI drafts, fact trace, exports and comparisons exist. Reuse #91/#92/#96–#100; broader tailoring remains partial. |
| 006 | Resume/job scoring and tailoring | Existing factor breakdown and explanation UI; reuse #113/#115, with deterministic positive/negative calibration and scope checks. |
| 007 | New-role alerts | Operational notifications and scheduled searches are adjacent, not proof of a complete new-role delivery flow. #205 remains NOT_RUN in the original authority; reuse #192/#205 for opt-in, dedupe, delivery and recovery. |
| 008 | Direct employer handoff | Original #268 is accepted in the inspected baseline. Parity is separately unverified. Preserve manual external-submission consent. |
| 009–010 | Private account/dashboard surface | Content inaccessible. No functional equivalence or absence inferred. |
| 011–012 | Data controls and saved-search persistence | TalentHop behavior unknown. Reuse #170/#192 when concrete gaps are observed. Billing/paywalls are not MJobs requirements merely because the reference offers them. |

Every feature in the catalog has original issue/requirement links where known, inspected source paths, dependency IDs, evidence IDs and an independent verification record. Existing code and legacy acceptance are not new parity acceptance. At this checkpoint all twelve parity verification records are NOT_RUN.

## Coordination and Fleet

Run: webpro-20261103T050255Z-1ba724. Source baseline: 7e86511f409fb9d1e2dc79904b170cc74b88cd3d. New code lives on work/webpro-20261103T050255Z-1ba724-talenthop until protected integration. Claim commits and release/continuation receipts live in #903/#904/#905; always requery refs.

The canonical board is [MulattoTech Project 1](https://github.com/users/MulattoTech/projects/1), identified by existing repository tools. The current CLI lacks read:project; no board changes succeeded and no duplicate board was made. The existing sync utility must not be run with expanded permission without authorization. After access is available, inspect actual fields and reconcile only under the integration claim.

Canonical Fleet source is MulattoTech/Fleet-Control-Hub. The companion change reads this catalog at an exact current-main SHA and independently refreshes safe GitHub metadata. It must show unavailable while the catalog is not on main. Open issues, claims and PRs are coordination evidence; merge/deployment/acceptance remain separate. Original fleet quotas, reserves and execution state are outside this change.

## Next authorized steps

1. Obtain direct authorization for reading the existing TalentHop job-seeker session; preserve the normal private-account boundary and avoid external writes.
2. Obtain a supported full-content HAR capture/export route and approved private raw storage. Do not relabel DOM or console output as HAR.
3. Exercise public and authorized journeys, then update stable IDs and coverage incrementally under a new claim. Add new IDs; do not reuse existing IDs for different capabilities.
4. Verify gaps against current MJobs source and existing issues before opening new feature tickets. Run literal tests, protected CI, serialized merge and guarded rollout.
5. Reconcile Project and deployed Fleet only when actual permission and delivery evidence exist.

Validation: python -m tools.verification.talenthop_catalog. Privacy procedure: [HAR_PRIVACY.md](HAR_PRIVACY.md).
