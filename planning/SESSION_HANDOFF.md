# MJobs checkpoint: private answer exports, 2026-11-03

Run `webpro-20261103T034313Z-1e2ff1`; [PR895](https://github.com/MulattoTech/MJobs/pull/895).
Branch `work/webpro-20261103T034313Z-1e2ff1-171`; tested source
`02db15748dae39b34848f6011327b6ce5c1cdf89`. MJ-REQ-171 claim owner
`db7d7b3320068adc89d7199346e45d6d8f371258`; requery its ref before writes.

## Verified and pending

Application-answer exports create POSIX owner-only files before writing and
retain permissions after atomic publication/process death. Matching uncommitted
recovery files are restricted and synced before metadata; mismatches and hard
links are preserved/refused. Scoped directories request0700. Fixed storage errors,
profile-scoped downloads/reopen, immutable hashes and no-store policy are covered.
Exact source: Linux45 PASS/0 SKIP; Windows39 PASS/26 subtests/6 POSIX SKIP,
with all six Linux counterparts PASS. Three baseline privacy failures are retained
in [source evidence](../docs/evidence/2026-11-03-webpro-1e2ff1-answer-privacy/source-execution.json).

PR893 protected merge `ea5fbf9f0c3c27ecb0d86b90168f276df0349c6b` is incorporated.
Its MJ-REQ-172 evidence/milestone is preserved unchanged; both171 and172 remain
PARTIAL. P0 PASS13/26 and P1 PASS107/116 are unchanged. No whole-parent PASS,
Windows ACL isolation, historical-file remediation or full auth/PII acceptance.
Protected latest-head checks, actual candidate validation, PR895 merge/main gates
and guarded deployment remain pending. Read PR895's final receipt for the future
head/merge/deployment hashes and releases; source files cannot contain future SHAs.

## Ownership and resume

Reconciliation uses integration owner `1cfd85d6abf061145bf803ac66d113341149a559`;
release it after this update and acquire a fresh claim before protected merge.
The older preparation owner `7eb3c57eca0cb0b56dee9541c14f1bc5ca6f8481` was released.
Deployment requires its separate claim and the existing guarded updater. Baseline
Brave/Docker readback was ready at63b843df; no changed production behavior is claimed.
Finish gates, release only this run's exact claims, then select independent work.
Source, separate venv and private receipts use the run ID under `C:\Tools\Dev`.
Preserve all other workers; rediscover live claims/PRs, including004/160/224/269/295.
[Previous main handoff](https://github.com/MulattoTech/MJobs/blob/ea5fbf9f0c3c27ecb0d86b90168f276df0349c6b/planning/SESSION_HANDOFF.md)
retains web-security, boundaries, scheduler, WWR, test-matrix and host-restart history.
No production test records, paid AI, external applications or fleet changes.
