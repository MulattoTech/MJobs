# TalentHop capability inventory and MJobs crosswalk

Epic [#904](https://github.com/MulattoTech/MJobs/issues/904), reconnaissance [#903](https://github.com/MulattoTech/MJobs/issues/903), Fleet [#905](https://github.com/MulattoTech/MJobs/issues/905).

The current checkpoint includes an **authorized signed-in UI audit**. It is still **PARTIAL**: no network capture, generated resume outcome, alert delivery, purchase, or employer submission was verified. The machine-readable source is [catalog.json](catalog.json); the detailed findings are in [UI_AUDIT_2026-11-03.md](UI_AUDIT_2026-11-03.md).

## Coverage and evidence

The audit reached **21 identified route patterns**, including the observed `/remote-jobs` redirect. **11 of 16 capability groups have recorded UI steps**. These denominators describe this discovered surface, not every feature the service might have. A read-only form inspection is explicitly distinguished from a persisted workflow or end-to-end result.

Discovery: **11 OBSERVED, 4 PUBLICLY CLAIMED, 0 INACCESSIBLE, 1 UNKNOWN**. MJobs source crosswalk: **3 ALREADY EXISTS, 12 PARTIAL, 1 MISSING**. **All 16 parity verification records remain NOT_RUN; none is newly accepted.** Existing code and original acceptance are separate from parity verification.

The prior public-only checkpoint and its access denial are retained as dated history. The user's subsequent direct authorization resolved private-session access. The existing signed-in Brave session was used for normal job-seeker navigation, read-only forms and one reversible bookmark test. That bookmark was removed, and the tracker returned to empty. Viewing the employer's original posting left Applied at zero.

Evidence:

- [Functional observations](../evidence/2026-11-03-webpro-bbbb6d-talenthop-ui/observations.json): steps, observed results, role, time and limits; no private account values or copied resumes.
- [Capture capability record](../evidence/2026-11-03-webpro-bbbb6d-talenthop-ui/capture-capability.json): actual capture limitation and artifact disposition.
- [Coverage guard negative control](../evidence/2026-11-03-webpro-bbbb6d-talenthop-ui/coverage-negative-control.json): baseline validator accepted marketing-only observed claims and inconsistent exercised totals.
- [Deployed Fleet consumer compatibility](../evidence/2026-11-03-webpro-bbbb6d-talenthop-ui/fleet-consumer-compatibility.json): the initial timestamp/blocker mismatch was rejected and corrected before integration; both the previous 1ee726fd reader and the independently deployed 0258903 reader accept 16 features. The receipt corrects the original deployment attribution. This is separate from a live refresh.
- The catalog validator now requires linked, bounded observation records for observed UI claims and reconciles the totals with individual route/journey records. It does not certify the truth of every observation, audit privacy automatically, or turn a UI observation into MJobs acceptance.

## HAR status

**BLOCKED — FULL_HAR_TOOL_UNAVAILABLE.** Authorization exists. The connected Browser API exposes DOM, screenshots, console messages and observed page assets, but no request/response recorder or full-content HAR export. The existing Brave process has no debugging endpoint, and the exposed workstation tools do not provide supported native DevTools UI capture.

**No raw HAR, partial network trace, captured sanitized derivative, capture byte size or capture hash exists.** DOM observations and sanitizer fixtures are not network evidence. No private raw archive was established because nothing was captured. Request schemas, response bodies, authentication flow, cache/service-worker behavior, redirects at the HTTP layer and timing measurements remain unknown.

A real DevTools Network export with content, or a supported recorder, is needed to finish this deliverable. Raw traffic must be retained privately with its actual hash, owner and expiry; only a reviewed derivative belongs in public artifacts. [HAR_PRIVACY.md](HAR_PRIVACY.md) describes the existing offline sanitizer, its limits and the distinction between capture completeness and redaction.

## Capability crosswalk

| ID | Reference evidence boundary | MJobs basis and next action |
| --- | --- | --- |
| 001 | Search, remote filtering and zero-result UI exercised | Existing search semantics; broader facet/pagination parity remains unverified. |
| 002 | Freshness/verification labels and report reasons observed | Source-candidate provenance, route trust and risk signals exist. No evidence establishes the reference's authenticity checks or report processing. |
| 003 | Preference form inspected | Existing preference/scoring and saved-profile modules; verify save/reload and downstream effects separately. |
| 004 | Tailoring setup visible; generation/review untested | Reuse #91/#92/#96/#97 and reviewed, fact-grounded draft workflows. |
| 005 | ATS/export claims unverified | Existing templates and export support; do not claim reference-output equivalence. |
| 006 | Existing score explanation and skill groups exercised | Existing factor breakdown; keep unassessed components distinct from zero and preserve factual uncertainty. |
| 007 | Delivery publicly claimed; no alert received | Reuse #192/#205 for opt-in, delivery, dedupe and recovery. |
| 008 | Original posting opened on employer Workday | Existing employer-first route trust; a view or handoff is not a submitted application. |
| 009 | Profile, notification, subscription and support controls inspected | Existing profile/settings UI; no automatic requirement to add a billing product. |
| 010 | Save/reload/unsave and tracker empty state exercised | Existing job dispositions and application tracking; keep saved and applied counts separate. |
| 011 | Policy states data rights; no export/delete outcome exercised | Existing settings transfer and #170 exports are partial overlap, not proof of full account-deletion parity. |
| 012 | Named saved searches/learned feedback not found in inspected UI | Existing saved-search support; reference behavior remains UNKNOWN, not absent. |
| 013 | Hourly minimum and salary-required behavior exercised | [#918](https://github.com/MulattoTech/MJobs/issues/918): expose existing salary-period semantics through main search API/UI and saved/history round trips. Related original #29 remains unchanged. |
| 014 | Base resume viewer and upload/create setup exercised | Reuse resume library/workspace and #91/#92/#99; parser, primary change, deletion and new output were not tested on the reference. |
| 015 | Recently Viewed list inspected | [#919](https://github.com/MulattoTech/MJobs/issues/919): add bounded per-profile job-view history. Existing #237 search execution history is a different event. |
| 016 | Email/SMS category switches inspected | Reuse #205 provider abstraction and operational notifications; channel persistence and actual delivery require separate verification. |

The catalog contains source paths, dependency IDs, issue/requirement links and a separate verification record for each feature. No TalentHop source, private API, asset, account data or job dataset is imported into MJobs.

## Coordination and Fleet

Current audit run: `webpro-20261103T073112Z-bbbb6d`. Inspected MJobs source baseline: `63cb1042e08375f7bda1f9dad378209cca856167`. Objective ownership and release receipts are in #903/#904; always re-read actual refs. This checkpoint does not change original or reference acceptance authorities.

Canonical board: [MulattoTech Project 1](https://github.com/users/MulattoTech/projects/1). The CLI's missing `read:project` scope remains a separate blocker; no Project update or duplicate board is claimed.

The existing Fleet Control Hub reads the catalog at an exact current-main SHA. Its independently deployed 0258903 reader accepts the added IDs and revised access/coverage counts; live ingestion still requires protected merge and refresh. A live receipt is required before claiming that refresh happened. Fleet now exposes bounded related-merge references; per-feature deployment/acceptance and heartbeat limitations remain tracked in #905; quota and execution policies are unchanged.

## Remaining work

1. Capture real full-content network evidence through a supported route, privately retain it, then sanitize and validate it.
2. Exercise authorized, affordable generation/output and delivery outcomes only within the governing scope; maintain explicit untested boundaries.
3. Implement #918 and #919 under separate atomic claims after checking active API/UI ownership.
4. Complete Project reconciliation when the existing board is accessible.
5. Verify each implemented capability on its actual candidate, protected merge and required deployment before changing acceptance.

## Shared integration prerequisite

[Receiving clock-fixture evidence](../evidence/2026-11-03-webpro-bbbb6d-talenthop-ui/shared-ci-clock-reuse.json) retains the receiving branch's actual date-boundary failure and 44 passing focused cases after exact authorized reuse of c6acb100. The four source-owner files are unchanged. The receipt also corrects the earlier scanner unittest collection count. Production search filters and all acceptance outcomes are unchanged; fresh protected successor gates remain required.
