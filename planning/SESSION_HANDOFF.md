# MJobs current checkpoint — 2026-11-02 Pro operational sprint

## Canonical acceptance

**P0: 12/26 PASS** (4 PARTIAL, 10 NOT_RUN), unchanged. **P1: 99/116 PASS** (14 PARTIAL, 3 NOT_RUN), up from the verified 96/116 baseline. Original requirements: **172/366 PASS**, 88 PARTIAL, 106 NOT_RUN. The separate reference authority remains **19/59 PASS**, 4 PARTIAL, 36 NOT_RUN, byte-for-byte unchanged. Whole-product readiness remains UNKNOWN/PARTIAL.

New full acceptances: **MJ-REQ-019 Source Capability Model**, **MJ-REQ-161 Error Categories**, and **MJ-REQ-179 Debug Capture**. [Criterion mapping](../docs/evidence/2026-11-02-pro-ops/criteria.json) and [delivery evidence](../docs/evidence/2026-11-02-pro-ops-delivery.json) preserve exact revisions, actual execution, historical failures and skips. MJ-REQ-004, MJ-REQ-017, MJ-REQ-167 and MJ-REQ-275 remain PARTIAL; MJ-REQ-017.S02 retains its existing PASS. No requirement definition, priority or denominator changed.

## Integration and production

[Source PR #881](https://github.com/MulattoTech/MJobs/pull/881) merged at `76ed9bdbbba82317c04bc5e1ca551e33d2f91f40`. [Operational PR #882](https://github.com/MulattoTech/MJobs/pull/882) merged at `f955e1b9f9ebeacf49fc3f96c0f57973c4ff0912`. [Transport fixture PR #883](https://github.com/MulattoTech/MJobs/pull/883) merged at **`2e0f3df8f6952088b39fc494f9cd38838b020af4`**. All five required checks passed for each exact proposed merge, with seven source job checkout identities and tested-tree equality verified.

The guarded updater deployed **`2e0f3df8f6952088b39fc494f9cd38838b020af4`**. Image ID: `sha256:70de560a8e3e1b30bc79e67074ebfb389cf1d4e94538683a1a7c510858eb15b4`. The seven application services and PostgreSQL/Redis were healthy; `/__deployment` and `/ready` matched the release. All seven application services agreed with `/settings` on the three numeric submission caps. External submissions remained OFF. The previous release/image and an integrity-verified pre-switch archive remain available. [Production receipt](../docs/evidence/2026-11-02-pro-ops/production-after.json) records exact identities and backup integrity; [public browser receipt](../docs/evidence/2026-11-02-pro-ops/public-browser.json) is separate from native fixtures.

The updater executable itself is installed from protected PR881 and hash-verified, with its previous script preserved. Its schedule, gate policy and rollback path remain intact.

**[Final acceptance publication and closeout receipt](https://github.com/MulattoTech/MJobs/pull/882)**: the final delivery update in the already-known source PR links the containing acceptance PR and records the exact final `main` SHA, issue #20/#162/#180 closure, alert #63 state, final live SHA and primary-checkout/worktree readback. The containing acceptance PR also retains that receipt in its final description. This source commit cannot self-contain a future publication commit hash. Source, canonical acceptance publication and live deployment remain separate identities; the receipt is finalized only after the acceptance merge.

## Verification and retained failures

Native exact proposed merges: 90 tests/83 subtests passed with 16 skips for PR881; 168 tests/113 subtests passed with 23 skips for PR882, including seven connected browser cases and a fresh UI build. PR881 additionally records nine diagnostic/privacy browser cases. New isolated PostgreSQL cases for source capabilities (4), configured caps (5) and structured categories (13) actually executed in protected CI. The fixture-only third proposed merge passed 35 native tests and 25 subtests without skips; its two integration modules also executed in hosted CI. Production code is byte-identical to the operational source. Full hosted counts and artifact hashes are in the three artifact receipts. Runs overlap and must not be summed.

Main `f955e1b9f9ebeacf49fc3f96c0f57973c4ff0912` failed Integration run 41371046952 and candidate run 41371046815 after its protected PR had passed. The failing test did not enter transport and omitted terminal-state diagnostics; its precise hosted cause remains unproven. A controlled pre-transport timeout reproduced the ambiguous assertion. The third PR prepares the real verifying SSL context before strict transport timing and adds terminal diagnostics and controls, while preserving the original deadline/cancel bounds and separate cold-SSL test. These failed main gates remain failures; the final source has separate successful main/deployment receipts. Earlier compatibility, diagnostic fixture and timing-oracle failures remain recorded. Final qualified runs pass; local PostgreSQL skips remain skips. The fresh WWR observation found two conflicting upstream groups among 100 entries/98 identities, followed by a separately recorded observer row-count error. No live WWR success is claimed. The existing approximately 978 kB frontend bundle warning remains.

## Resume work and ownership

Verify latest main, this acceptance PR final receipt, issue #20/#162/#180 closure and live SHA; then implement remaining MJ-REQ-275 notification/profile configuration and MJ-REQ-004 complete operation bounds. Keep MJ-REQ-167 protected recovery and WWR upstream conflicts explicit.

The minimatch 11.0.3 override is deliberately scoped to the two inspected ESLint consumers and tested beyond their declared semver ranges. Remove it when upstream consumers support a patched release. Do not silently suppress conflicting WWR records or rewrite historical failure evidence.

Other original P1 requirements remain PARTIAL: MJ-REQ-224 needs the complete contract matrix across every supported application adapter; MJ-REQ-267 needs fresh automatic employer-route discovery before preparation; MJ-REQ-269 needs an AI-generated document consumer with fact references. They were PARTIAL at the verified baseline and remain unchanged. The deterministic trace, explicit route resolver and controlled mock journey do not establish those broader consumers.

The source and exact-merge worktrees remain under `C:\Tools\Dev\MJobs-pro-ops-20261101`, `MJobs-pro-minimatch-20261101`, `MJobs-pro-ops-mergecheck-20261101` and `MJobs-pro-operational-mergecheck-20261102`. The fixture source and proposed-merge worktrees are `MJobs-pro-transport-fixture-20261102` and `MJobs-pro-transport-mergecheck-20261102`. The acceptance worktree is `MJobs-pro-ops-acceptance-20261102`. Final receipt records cleanliness and primary checkout fast-forward. Owned pre-rebase backup branches preserve intermediate history; ignored outputs contain reproducible validation intermediates. No unknown work was reset or cleaned.

Portable checkpoint: [pro-ops-delivery-20261102](agent_handoffs/pro-ops-delivery-20261102.json). One integration owner, no standing worker claims or unattended fleet. Keep real employer submissions OFF; no paid AI calls, destructive live restore, unrelated service changes or host restart were performed or authorized by this handoff.
