# GitHub automation and integration policy

## What runs

| Gate | Trigger | Evidence |
|---|---|---|
| CI - fast | PR and main | Unit tests, Vitest/TypeScript/build, public workflow policy, documentation build and canonical validators. |
| Integration | PR and main | Full Python integration directory with disposable PostgreSQL/Redis and explicit guarded recovery database. |
| Connected acceptance | PR/manual/weekly | Existing controlled Compose, mock ATS, browser journey and restart/persistence checks. |
| Browser regression | Manual/twice weekly | Every browser test file assigned to one of four disjoint shards. Known failures stay failures. |
| Dependency review | PR | New high/critical dependency vulnerabilities block the check. |
| CodeQL | GitHub-managed | Python, JavaScript/TypeScript and Actions analysis. Open findings still need triage. |
| Documentation site | Main/manual | Curated static docs only; no runtime databases, private logs or applicant data. |
| Release validation | Manual/version tag | Guarded source revision, tests, image build and provenance-ready publication policy. |

All third-party actions are pinned to commit SHAs. Defaults are `contents: read`; only trusted publication jobs may request narrowly scoped additional permissions. Superseded PR runs are cancelled, jobs have explicit timeouts, and test reports have short retention. Required check names remain stable.

## Native merge gate

Use short-lived PRs and native GitHub required checks/auto-merge. Do not create a workflow that approves its own changes. Automatic successful checks are not a human review, license audit or whole-product PASS. Keep an audit trail of maintainer-authorized integration and exact tested revision.

Required gates are enabled only after they run successfully. Do not require a filtered workflow that never starts on some PRs. A skipped/failed/cancelled sub-job cannot pass `CI / required`. Full browser tests remain separately visible until their known product failures are fixed; the release gate does not conceal them.

## Projects and issues

The public Project mirrors issue priority, area, category and recorded acceptance. `tools/maintenance/sync_project.py --apply` updates it with the existing local GitHub login. Call it after meaningful accepted batches, not after every commit. The matrices remain acceptance authorities; moving a card cannot make a feature PASS.

No broad personal token is copied into repository secrets. Project background syncing requiring a dedicated scoped credential is not silently enabled. GitHub's built-in project views can filter Priority, Area and Acceptance without creating a duplicate issue system.

## Dependency practice

Group minor/patch updates weekly and cap concurrent PRs. Keep major updates separately reviewable. Run real gates before merging. Dependency alerts, source-code alerts and secret scanning are distinct checks. A green CodeQL job may still contain security alerts.

## Troubleshooting and measurements

Use `gh pr checks <number>` and `gh run view <run> --log-failed` for the exact candidate. Record workflow, run, attempt and source SHA; do not combine unrelated failure tails. Inspect test artifacts for counts/skips. Track turnaround time, repeated flaky tests, open high-severity findings, and actual P0/P1 acceptance rather than commit volume.
