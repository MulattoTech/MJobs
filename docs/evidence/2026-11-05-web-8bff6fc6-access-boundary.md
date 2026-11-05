# MJobs public-access documentation correction

Run: web-20261105T024512Z-8bff6fc6-0a03-44d0-b6f2-3c98498e9165.
Scope: MJOBS-ACCESS-BOUNDARY-DOCS, related to MJ-REQ-172 / issue #173.
Base main: d6650e4bc9bfd784bd6e3a346c793c372f02518a.
Atomic owner: 9d7bdfaac60a12aacb5d2194b45a03d35cf5c52d.
Claim creation: HTTP201 and exact readback at 2026-11-05T02:58:52.2224318Z.

## Observation and correction

The deployment guide described the public application as restricted to the
owner's email. A read-only Cloudflare API inspection on 2026-11-05 UTC returned
one exact-domain MJobs self-hosted Access application and its complete two-policy
list (page 1, total 2). Both policies have decision allow:

- MJobs owner only: one email selector.
- M-TechLabs centrally approved accounts: one external-evaluation selector,
  with a one-hour session override.

No require/exclude selectors or Bypass policy appeared in those two returned
policies. The app has HttpOnly enabled, a twelve-hour default session and OPTIONS
preflight bypass disabled. No personal selector values, audience identifiers,
tokens, evaluation URLs or account identifiers are included in this receipt.

The guide now describes Access as the public entry gate, records the dated
policy observation and explicitly leaves effective evaluator membership and
authenticated isolation unverified. The existing statement that this PERSONAL
preview has no native multiuser authentication remains material.

The MJobs tunnel reported healthy with four connections. This is tunnel
availability, not authentication or application-journey acceptance.

## Separate read-only release baseline

At 2026-11-05T02:57:51.5381149Z the local origin reported:
- Source revision 0d3fbc5749fba8c682033a343a7ec9695e4dd743.
- Version 0.1.0-dev; PERSONAL mode; PostgreSQL backend.
- Configuration, database, queue, scheduler, schema and all five worker queues
  passed readiness.
- All nine MJobs Compose containers were healthy.
- The existing updater's last result was zero.

This run did not deploy that revision. The source and release observations are
independent of the documentation change and of PR933 integration.

## Limits and validation

The connected Browser tool rejected initialization because the originating
browser context is unavailable. Signed-in Brave behavior, allowed/denied user
journeys, the evaluator's effective population and per-profile isolation are
NOT RUN. No alternate browser/session or new login was used.

GitHub Project readback is blocked by the existing token's missing read:project
scope. No authorization scope was changed. No Project/Fleet synchronization or
acceptance change is claimed.

Documentation was checked against the complete returned policy metadata and
the preserved deployed-source observation. Repository validation, whitespace,
commit provenance and protected publication results are recorded on the PR.
No product behavior changed, so no new behavioral tests were introduced.
No Cloudflare policy, evaluator, tunnel, runtime, applicant data, quota or
emergency setting was mutated. MJ-REQ-172 remains PARTIAL; issue #173 remains
open. Rollback is a normal documentation revert.
