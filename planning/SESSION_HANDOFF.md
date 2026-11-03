# MJobs checkpoint — private answer exports, 2026-11-03

Run `webpro-20261103T034313Z-1e2ff1` owns MJ-REQ-171 through
`refs/heads/webpro-claims/MJ-REQ-171`, owner commit
`db7d7b3320068adc89d7199346e45d6d8f371258`. Requery the ref before resuming.
Branch: `work/webpro-20261103T034313Z-1e2ff1-171`.
Tested source: **`02db15748dae39b34848f6011327b6ce5c1cdf89`**.
Baseline main/deployment: `63b843dfe2706e6a5ac1d567af7e5acb01e134a7`.

## Verified work

Application-answer exports create private POSIX files before writing. Recovery
accepts only exact matching regular files with one link, restricts and syncs that
file descriptor before recording the document, and preserves mismatched or
unrelated files. New scoped directories request0700; existing directories are
unchanged. Storage failures expose fixed safe classifications.

Exact-source Linux: **45 PASS /0 SKIP**. Native Windows: **39 PASS /6 POSIX SKIP /
26 subtests PASS**; all six counterparts passed Linux. Baseline three permission
failures are preserved. Reopen/download scope, immutable hashes, no-store policy,
process-death recovery and failure rollback passed. No human independent review
is claimed. [Execution and limitations](../docs/evidence/2026-11-03-webpro-1e2ff1-answer-privacy/source-execution.json).

## Accounting and release

Only **MJ-REQ-171: NOT_RUN → PARTIAL**, zero new PASS credit. P0 PASS13/26 and
P1 PASS107/116 at this branch's verified baseline; refresh canonical main before
reporting current global totals. Full PII protection, Windows ACL isolation and
historical export/backups remediation remain incomplete.

Accounting was prepared under exclusive integration ref
`7eb3c57eca0cb0b56dee9541c14f1bc5ca6f8481`; release it during CI and acquire a
new integration claim before merge. **Protected CI, actual merge candidate,
merge, main verification and guarded deployment are pending.** Read the
containing PR's final receipt for future head/merge/deployment hashes and claim
releases. A source commit cannot contain its own future merge hash.

Brave baseline readback showed Services ready, External submission OFF and
exact63b843df; Docker document storage is a volume. This is not proof the fix is
live. No production records, paid AI, external applications or fleet settings
were changed.

## Resume

Preserve other workers'004/172/224/269/295 ownership; requery claims and open PRs.
Finish current PR gates and guarded release, then release only this run's exact
claims. Source worktree and separate environment/evidence directories share the
run ID under the development directory. All earlier boundary, scheduler,
test-matrix, WWR and host-restart limitations remain in the
[baseline handoff](https://github.com/MulattoTech/MJobs/blob/63b843dfe2706e6a5ac1d567af7e5acb01e134a7/planning/SESSION_HANDOFF.md)
and dated evidence; none are superseded by this file-storage slice.
