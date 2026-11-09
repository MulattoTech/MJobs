# MJ-REF-024: follow-up draft context and clipboard repair

Issue: #394. Run: web-20261109T031106Z-31f99071bbd9.

## Problem and change

A pending clipboard completion could clear or replace feedback for a newer quiet-day context. Draft and API-error state could also appear in the first committed render after the context changed, before passive effects reset it.

The query result and clipboard state now belong to a keyed child for each profile/quiet-days selection. A sequence guard ignores superseded copy completions. The selector stays outside that child so its DOM node and keyboard focus survive a threshold change. The exported view contract, endpoint, reported-history attribution, character limits and copy-only controls remain intact.

## Exact-revision evidence

- Implementation and tested revision: `b62a0d2293db3b6c3182af67c7dd6170ceea62d7`.
- Baseline main: `971ba931418fc271e7a652e27c288e1776448b6d`.
- New regression suite against original source: **7 failed, 6 passed**.
- Fixed source: **13 tests passed**, including mounted deferred-promise races and first-commit observations.
- Strict targeted TypeScript, catalogue, progress validation, dashboard currentness, whitespace and source commit provenance: **PASS**.
- Independent source/test review: no blocking findings; reviewer execution NOT_RUN.
- Exact commands, Git blob IDs, environment and individual outcomes: adjacent JSON file.

The primary dependency cache initially lacked JSDOM and executed zero tests. Matching-version packages were privately copied from an existing cache; no dependencies were installed or shared dependencies modified. Initial TypeScript invocation/config discovery and unsupported test-query options were corrected before final verification.

## Acceptance limits and follow-up

This is scoped repair evidence, not a fresh full-requirement PASS. Canonical matrices are unchanged. Full build, real-browser acceptance and deployment are NOT_RUN under the unavailable heavy gate.

Normal application-level profile switching already unmounts the component. The tests establish direct component context isolation, not an observed leak through that normal application path. Already-issued browser clipboard writes are not cancellable; only their obsolete UI feedback is discarded.

The mounted follow-up list still lacks invalidation when application tracking status changes. That separate user-journey gap requires the owner of `apps/web/src/main.tsx`; this branch does not edit the shared consumer.

Rollback: a normal protected revert of the source commit. There is no schema or persisted-data change. External sending and real application submission remain outside this feature.

## Verified historical scanner dependency

The all-history scanner reported one source-file checksum in another branch.
Root and an independent reviewer recomputed the referenced immutable Git blob
and confirmed equality. After the peer released the exclusive scope, this branch
incorporated exception-only commit a796ffb478833fbcf87ff79892b6047108336226 unchanged via cherry-pick.
The resulting ignore blob is e9591c3b04952b1752c020f5df909483227c3edd.
Only one immutable commit/path/rule/line fingerprint is excepted; no scanner rule,
workflow or directory-wide exclusion changed. Source and test blobs remain identical
to the locally tested implementation. Fresh hosted scanning on the final head is pending.
The companion JSON records exact provenance and the comparison result without
repeating the flagged digest.
