# Resumed MJobs delivery — 2026-11-02

Three protected source PRs deliver seven additional literal P1 acceptances. Feature source **`d86a5bf79e883fdd40b750ad843a363960f35c43`** was deployed at **16:31:13 UTC**. This directory records source delivery separately from its containing acceptance PR. That PR's final description records the later publication merge, exact checks, final deployment and issue-closure readback.

## Acceptance

| Authority | Resumption baseline | Accepted result | Still incomplete |
| --- | --- | --- | --- |
| P0 | 12/26 PASS | **12/26 PASS** | 4 PARTIAL, 10 NOT_RUN |
| P1 | 99/116 PASS | **106/116 PASS** | 7 PARTIAL, 3 NOT_RUN |
| Original requirements | 172/366 PASS | **179/366 PASS** | 81 PARTIAL, 106 NOT_RUN |
| Separate reference gaps | 19/59 PASS | **19/59 PASS** | 4 PARTIAL, 36 NOT_RUN |

The original prompt began at P1 92. Earlier interrupted work had reached 99; this continuation contributes the next seven. The105 target is exceeded; the 110 stretch target is not reached. Definitions, priorities and denominators are unchanged. Whole-product readiness remains PARTIAL/UNKNOWN.

| New acceptance | Issue | Delivered behavior and evidence |
| --- | --- | --- |
| 162 — Database Design | [#163](https://github.com/MulattoTech/MJobs/issues/163) | Relational ownership, constraints and migrations; deployed read-only validation of 50 tables / 84 foreign keys. [Mapping](configuration-and-schema.json), [schema](production-schema-d86.json). |
| 167 — Data Backup | [#168](https://github.com/MulattoTech/MJobs/issues/168) | Database, uploads, generated documents and important configuration recovery, including an encrypted private companion. [Criteria](backup-criteria.json), [artifacts](pr888-artifacts.json). |
| 175 — Secret Management | [#176](https://github.com/MulattoTech/MJobs/issues/176) | Encrypted storage, masked metadata, safe errors and credential canaries through API/browser/log boundaries. [Receipt](pr887-protected-merge.json). |
| 208 — Dry-Run Mode | [#209](https://github.com/MulattoTech/MJobs/issues/209) | Fresh discovery, scoring, documents and Chromium preparation; durable DRY_RUN_COMPLETE and zero submissions. [Command](delivered-dry-run-d86.json). |
| 267 — Direct Employer Route Discovery Before Apply | [#268](https://github.com/MulattoTech/MJobs/issues/268) | Automatic bounded resolution attempt, current employer/domain/tenant validation, durable status/hash audit, unchanged selected route and fresh consent/cancellation/policy checks. [Receipt](pr886-protected-merge.json). |
| 275 — Configuration | [#276](https://github.com/MulattoTech/MJobs/issues/276) | All 11 original UI/environment categories exercised; seven services agree with safe runtime settings. [Mapping](configuration-and-schema.json), [runtime](production-runtime-d86.json). |
| 288 — Verification Utility | [#289](https://github.com/MulattoTech/MJobs/issues/289) | Observed production readiness plus 13 fixture checks, stable matching clean source/deployment and owned cleanup. [FULL command](delivered-full-d86.json). |

[delivery.json](delivery.json) contains the full literal decisions and evidence index. [Historical PARTIAL notes](prior-acceptance-notes.json) and earlier failures are preserved.

## Protected integration and execution

| Source PR | Exact native proposed-merge result | Actual merge |
| --- | --- | --- |
| [#886](https://github.com/MulattoTech/MJobs/pull/886) | 406 PASS, 4 SKIP, 372 subtests; tested 219bc9eb | `9a040029ecd5515da20c0b8fd1ef4c412c5fec1d` |
| [#887](https://github.com/MulattoTech/MJobs/pull/887) | 164 PASS, no skips, 166 subtests; tested 82a1f59b | `2c6a93f3eeecd5d6777bec25a18855a7bf8ac310` |
| [#888](https://github.com/MulattoTech/MJobs/pull/888) | 144 PASS, 3 SKIP, 150 subtests; tested b83d524a | `d86a5bf79e883fdd40b750ad843a363960f35c43` |

All five protected gates and CodeQL passed for each source PR; tested trees equal merged trees. PR888 hosted unit tests: **3,385 PASS / 53 SKIP / 3,724 subtests**. Its disjoint integration shards: **2,545 PASS / 39 SKIP / 1,495 subtests**. PR886 web validation: **1,102 tests in 154 files**, including three notification component tests. Runs overlap and must not be summed.

Each PR888 Windows skip has separate Linux proof; Linux NTFS skips have Windows proof. The broader stack-restart/company-monitoring skip and Compose onboarding **PARTIAL** remain open.

These deployed public commands both exited zero:

```text
python -m cli.verify_readiness --dry-run --fixture --json
python -m cli.verify_readiness --full --fixture --api http://127.0.0.1:18801 --json
```

DRY_RUN passed 11/11 and stopped before final submission. FULL passed 13/13, including one owned mock confirmation and duplicate protection, with separate production PostgreSQL/Redis/RQ/scheduler/schema observations before/after. Both verified retained process containment and cleanup. [Command exits](delivered-command-manifest-d86.json) and [byte provenance](native-receipt-provenance.json) distinguish native CRLF output hashes from published LF JSON.

## Deployment, failures and cleanup

[Guarded delivery](guarded-delivery-d86.json) binds all four main gates, exact source, updater hash, verified backup and retained rollback. The current PERSONAL backup has three manifested files, as do the five nearby pre-switch archives. Document recovery is separately proved with the 84-file disposable fixture archive. No destructive production restore occurred.

The first d86 candidate failed on a GitHub HTTPError before image build; its status code is unknown. One diagnosed job rerun passed after independent gates and fresh API reads. [Gate recovery](deployment-gate-recovery.json) adds limited transient-read retries, safe diagnostics, original-deadline accounting, newest-run selection and conflicting-duplicate rejection. It neither proves the original error transient nor closes004. The progress-record CLI's post-write validation exception is also corrected; its first failure and exactly-once journal state are retained.

[Cleanup](environment-cleanup.json) records ownership and published-blob checks before cleaning 33 owned dirty files. The disposable PostgreSQL container/attached volume and task-private Temp were removed. **13 unrelated fleet worktree changes were preserved.** The final PR receipt records post-publication cleanliness.

[Cloudflare](cloudflare-and-browser.json) remained healthy with four tunnel connections and existing Access policies; no configuration changed. Authenticated public Browser observation is **NOT_RUN** because the originating context was unavailable. Native Chromium fixtures are separately scoped.

## Next work and limitations

- **004/P0:** genuinely blocked database cancellation/deadline behavior, durable fencing and cleanup; broader browser protocol/filesystem/lock bounds.
- **008/P0:** real RQ/Redis interruption/recovery and physical restart proof.
- **292/P0:** the original complete connected journey; fixture 11/13 is not the 60-step requirement.
- **171/172/174/P1:** broader PII protection, private-route authentication/authorization and a genuinely used ORM persistence path remain NOT_RUN.
- [WWR](wwr-bounded-replay.json) still has two conflicts among 100 entries / 98 identities. Both replay orders safely reject without changing six job tables; source availability is not accepted.
- [Alert 63](dependency-followup.json) was already fixed before this continuation. The scoped minimatch override remains pending upstream compatibility.

External employer submissions remained OFF. No paid AI calls, host reboot, unrelated process termination or external agent fleet was used. Historical timing, backup-path, privacy-fixture and source-identity failures remain in the delivery record.
