# MJobs current checkpoint — 2026-11-01

## Verified acceptance

This checkpoint records **92/116 P1 PASS**, up 43 from 49; 18 remain PARTIAL and 6 NOT_RUN. Original acceptance is 165/366; the separate reference authority remains 19/59. Whole-product readiness remains UNKNOWN/PARTIAL.

Source: `ca1e62c6dc64ef4149bba577f02973684e259e16`, protected [PR #875](https://github.com/MulattoTech/MJobs/pull/875), merged as `07eab582dbce29de49cde2180a39b031c3ad5ab2`. Every source-testing required job checked out proposed merge `f68edc8cce2013f4ccfc5df209862dc40651fb9e`; its tree equals the integrated source tree.

[Primary evidence](../docs/evidence/2026-11-01-pro-p1-delivery.json) contains the exact 43 IDs, per-criterion records, retained failures, hashes, run links and review limits. Publish this record through [PR #877](https://github.com/MulattoTech/MJobs/pull/877), then attach the issue-closure and deployment receipts to that PR. Verify those receipts before claiming deployment.

Dependency follow-up: [PR #876](https://github.com/MulattoTech/MJobs/pull/876) merged at `25e3f25968ce8e6148ede67fae6e029414567567` after all five required gates on proposed merge `7c09ae3f21690dc1d66d6bcb71fb9a3497f92e2b`. It upgrades pypdf to 6.21.4 for seven new high-severity advisories. A separate minimatch development-dependency advisory remains. The final acceptance PR must pass again against this integrated base.

## Executed verification

- Fast CI: 3,147 passed / 51 skipped. Integration: 2,414 passed / 35 skipped. No failures; subtests are reported separately in evidence.
- All 196 named acceptance nodes and 17 mandatory real PostgreSQL/RQ cases passed. The additional opt-in STK01 case remains SKIPPED, with its narrow scope decision retained.
- Frontend: 1,000 passed. Required hosted browser: 5 passed. Final Windows criterion browser: 15 passed plus 5 subtests. The uploaded-comparison module also passed 2 tests on the final source.
- Remotive (13 jobs), RemoteOK (99) and Greenhouse (19) passed live capture plus two production collections and two database reopens. Official Greenhouse careers/ATS/tenant discovery passed. No employer submissions or paid AI calls.

## Remaining work

Prioritize MJ-REQ-254 (21 modules / 32 unbounded injected-fetch dispatches), 275 (configuration surfaces), 179 (bounded DOM capture), 267 (fresh employer-route discovery), 224 (application-adapter journeys), and 269 (AI-generated document fact trace). WWR's live feed has conflicting content for one native identity; its `SOURCE_DUPLICATE_JOB_ID` failure remains recorded. P0 remains 12 PASS / 4 PARTIAL / 10 NOT_RUN.

## Operating boundaries

The 7% emergency cutoff and standalone/fleet isolation are unchanged. Preserve all unrelated work, sessions, volumes and backups. Use one integration owner and normal protected PRs. MJobs micro rollout uses the existing verified-main updater, quiescent backup and previous-image rollback; verify the exact deployed commit and `/ready` before reporting deployment. The previous release at this checkpoint was `53203ea2` and healthy.
