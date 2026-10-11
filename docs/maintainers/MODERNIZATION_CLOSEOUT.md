# MJobs modernization: verified delivery and next development step

The original modernization is merged in [PR #528](https://github.com/MulattoTech/MJobs/pull/528). Its verified continuation is merged in [PR #533](https://github.com/MulattoTech/MJobs/pull/533), main revision `253db58e5e5eab13f3c29d4bcb84d62dc4f12bf2`. This handoff records what exists; it does not claim the application is production-ready.

## Open the delivered work

| Resource | Location |
|---|---|
| Product documentation and screenshots | [Published documentation](https://mulattotech.github.io/MJobs/) |
| Install and use the two isolated modes | [Installation](../INSTALLATION.md) |
| Prioritized delivery board | [MJobs Project](https://github.com/users/MulattoTech/projects/1) |
| Required verification and scheduled checks | [GitHub Actions](https://github.com/MulattoTech/MJobs/actions) |
| Security backlog | [CodeQL triage #524](https://github.com/MulattoTech/MJobs/issues/524) |
| Complete browser regression baseline | [Browser repairs #532](https://github.com/MulattoTech/MJobs/issues/532) |
| Privately preserved Git recovery | [MJobs-Archive release](https://github.com/MulattoTech/MJobs-Archive/releases/tag/recovery-20261011), owner access required |

## Six-phase result

Recovery bundles remain privately preserved and independently checksummed. The 12 public recovery tags and old public bundle release were retired without rewriting main. The intact archival worktrees and original Git metadata remain in private local recovery storage; 924 clean detached registrations were retired, while ambiguous/dirty/external work was preserved.

CI now has stable required fast gates, three PostgreSQL/Redis integration shards, connected Compose/restart checks, four full-browser shards, dependency review, pinned secret scanning, CodeQL and bounded artifacts. The Project has 451 issue cards after the closeout sync, with priority/category/area/acceptance and focused views. README and seven-page documentation use actual fixture screenshots. Original source has Apache-2.0 licensing, NOTICE and a guarded, unreleased version policy. No production artifact was published while acceptance is incomplete.

The continuation repaired a real Dependabot runtime-manifest failure without changing package versions. Authoritative pins live in `requirements.txt`; `requirements.lock` remains compatible. Cache keys, Docker, environment compatibility and the CPU-only guard follow the actual included pins. Runtime-major upgrades require deliberate compatibility work.

Automated documentation reviews are narrowly scoped, explicitly identified as bot verification, and require all five exact-head checks. They never run pull-request code with a write token, approve workflow/source/licensing/requirement changes, invent a human review, or bypass merge protections. Code PRs use native required checks and optional auto-merge. See [automation policy](AUTOMATION.md).

## Verification, not estimated completion

PR #533's local full unit baseline passed **1,339 tests and 688 subtests**. The automated-review policy passed **14 Node tests**. Dependency includes, initial/resume launcher previews, public documentation/policy, host safety and CPU-only guards passed. Its exact-head hosted Python/web/policy, three integration shards, connected Compose, dependency review, secret scan, CodeQL analysis and devcontainer smoke all passed before native auto-merge.

Full product verification is still incomplete. The completed browser run on prior main `4f208168` reported **211 JUnit cases and 44 failures**, zero errors/skips, with subtests included; all failing identifiers are recorded in #532. The security audit baseline has **35 high and three medium CodeQL findings**; analysis success does not resolve them. Original acceptance remains **106/366 PASS**, P0 **7/26**, P1 **39/116**; reference acceptance remains **19/59 PASS**. The independent P0/P1 gate correctly reported **96 unmet selected requirements** and refused readiness.

## Start the next Sol Medium sprint

Use one integration owner in `C:\Tools\Dev\MJobs`; do not start this loop beside another session writing the same checkout. Keep personal configuration, encryption keys and populated volumes untouched.

From the canonical checkout, use PowerShell 7:

```powershell
# Preview exact arguments. This does not launch Codex.
pwsh -NoProfile -File .\Start-CodexSprint.ps1

# Explicitly start the new authorized goal with Sol Medium.
pwsh -NoProfile -File .\Start-CodexSprint.ps1 -Run

# Read status only.
pwsh -NoProfile -File .\Start-CodexSprint.ps1 -Status

# Resume the same stored goal and verification contract after interruption.
pwsh -NoProfile -File .\Start-CodexSprint.ps1 -Run -Resume
```

The installed AFK-DEV runner creates supported fresh-context iterations with no arbitrary iteration ceiling and a three-stagnant-iteration breaker. It does not remove account, runtime, approval or external-service limits. Resume intentionally does not replace the stored goal with a changed prompt. No autonomous feature-development loop was started by this modernization.

For an interactive session, use the checked-in [next /plan](../../prompts/codex/next-plan.md), [next /goal](../../prompts/codex/next-goal.md), and [full main directive](../../prompts/codex/next-main.md). Start with genuine exploitable security/integrity paths, then browser/profile/review failures, durable document comparison, saved-search-driven discovery, employer enrollment/deduplication and coherent supported ATS preparation. Reuse working infrastructure; do not spend another sprint rebuilding CI or bookkeeping.

The completion oracle is `python -m tools.ci.release_gate --scope p0p1 --online`, plus each chosen issue's acceptance tests. Real employer submissions remain OFF. A green documentation review or passing build is not full application acceptance.
