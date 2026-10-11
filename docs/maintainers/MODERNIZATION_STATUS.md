# Public modernization - execution record

The six-stage implementation is tracked in [PR #528](https://github.com/MulattoTech/MJobs/pull/528). Use that PR and the latest exact-main workflow runs for live publication status. This record does not promote product acceptance.

| Phase | Implemented work and verification |
|---|---|
| 1. Recovery and public safety | Two Git bundles privately uploaded, downloaded and SHA-256 verified. The historical public bundle release and 12 recovery tags were retired only afterward. No main history rewrite. The classified all-ref scan covered 2,790 commits and reran without unmatched findings. |
| 2. CI | Independent fast checks, aggregate required gate, disposable three-shard PostgreSQL/Redis integration, controlled Compose acceptance, four full-browser shards, dependency review, pinned secret scan and JUnit summaries. A real hosted health-command quoting failure was corrected and protected by policy validation. |
| 3. GitHub practices | Actions, CodeQL, secret scanning/push protection, private vulnerability reporting, Discussions, a public Project with 450 initial issue cards, four custom fields and six focused views. Integration uses native status checks/auto-merge rather than a workflow that approves itself. |
| 4. Repository hygiene | 924 inactive clean detached worktrees moved intact into private recovery storage, with Git index/HEAD metadata retained; registrations fell from 1,000 to 76. Dirty/branched/external work remained preserved. Obsolete prompt filenames now point to one current contract. |
| 5. Public documentation | README with three real fixture screenshots, installation/upgrades, development, architecture, privacy and automation guides. Seven-page allowlisted static site with no runtime export. Local link/policy checks, site build and export-boundary tests passed. |
| 6. Release and continuation | Original-source Apache-2.0 license, NOTICE, unreleased version/changelog, guarded image/release pipeline, exact-head acceptance gate, and Sol Medium plan/goal/main prompt plus a dry-run-first installed AFK-DEV launcher. No fabricated product version or external submission. |

## Verified local baseline

- Full unit run before the final helper additions: **1,321 passed and 684 subtests passed**.
- Frontend: **768 passed**, TypeScript/Vite production build passed. Local Node 25 produced a supported-engine warning; hosted CI uses Node 24.
- Focused policy/export tests after hardening: **12 passed**; static site, catalogue/skill/reference-policy checks passed.
- PowerShell launcher parsed successfully; the installed AFK runner accepted the continuation invocation in dry-run mode. An actual feature-development loop was not started.

## Public entry points

- [Reviewable modernization PR](https://github.com/MulattoTech/MJobs/pull/528)
- [Delivery Project](https://github.com/users/MulattoTech/projects/1)
- [GitHub Actions](https://github.com/MulattoTech/MJobs/actions)
- [Documentation site](https://mulattotech.github.io/MJobs/) - deployed by the trusted main-only Pages workflow
- [Discussions](https://github.com/MulattoTech/MJobs/discussions)

## Boundaries that remain real

The scan's 67 candidates were 60 SHA-256 metadata values, three verified Git object IDs, two documented Git commit references and two explicit test mocks. Only their exact fingerprints were baselined. A pattern scanner is not proof that every possible secret, personal datum or copyright issue has been audited.

The CodeQL baseline still contains 35 high and three medium open findings (#524); a passing analysis run does not fix them. Full browser acceptance has existing product failures. Release publication stays blocked until actual acceptance, exact-main workflows and security criteria pass. `VERSION=0.1.0-dev` is not a released product.

The private recovery directory contains complete worktree inventories, intact retired folders, Git metadata and verified bundles. Public pointer deletion cannot recall downloaded copies. Remaining dirty or externally used worktrees are not erased to produce a misleadingly small count.

Projects synchronization uses the owner's existing local GitHub login and is incremental; no broad personal token was installed in public repository secrets. Run it after accepted implementation batches. Native auto-merge is not a human approving review.

## Verified continuation

[The linked closeout and launch guide](MODERNIZATION_CLOSEOUT.md) records PR #533, the repaired dependency automation, verified review policy, exact regression baseline and new/resume AFK launch commands.
