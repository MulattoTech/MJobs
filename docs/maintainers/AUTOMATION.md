# GitHub automation and integration policy

## What runs

| Gate | Trigger | Evidence |
|---|---|---|
| CI - fast | PR and main | Unit tests, Vitest/TypeScript/build, public workflow policy, documentation build and canonical validators. |
| Integration | PR and main | All Python integration files across three isolated shards with disposable PostgreSQL/Redis and explicit guarded recovery database. |
| Connected acceptance | PR/manual/weekly | Existing controlled Compose, mock ATS, browser journey and restart/persistence checks. |
| Browser regression | Manual/twice weekly | Every browser test file assigned to one of four disjoint shards. Known failures stay failures. |
| Dependency review | PR | New high/critical dependency vulnerabilities block the check. |
| CodeQL | GitHub-managed | Python, JavaScript/TypeScript and Actions analysis. Open findings still need triage. |
| Documentation site | Main/manual | Curated static docs only; no runtime databases, private logs or applicant data. |
| Release validation | Manual/version tag | Guarded source revision, tests, image build and provenance-ready publication policy. |

All third-party actions are pinned to commit SHAs. Defaults are `contents: read`; only trusted publication jobs may request narrowly scoped additional permissions. Superseded PR runs are cancelled, jobs have explicit timeouts, and test reports have short retention. Required check names remain stable.

## Publication capacity and execution capacity

The [PR publication policy](../PR_PUBLICATION_POLICY.md) defaults to 32 concurrently
open development PRs, including drafts, with a configurable soft warning. Only
authenticated dependency automation is excluded. Use the read-only live queue
preflight; report unknown authorship/category, backlog, conflicts and exact-head CI.
The 32 publication slots are independent of expensive CI capacity. Aim for 4–6
expensive jobs where supported; existing per-run matrices and workflow/PR concurrency
groups do not enforce an aggregate repository semaphore. Preserve all required and
security checks, unrelated runs, billing and runner configuration. One integration
owner still serializes protected integration after applicable authorization.

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


## Verification-only documentation approvals

`verified-review.yml` is an explicitly identified bot review, not a human/code/security review. It runs only trusted `main` policy, never PR code or downloaded artifacts. It can approve owner-authored, same-repository documentation-only PRs after all five current required GitHub Actions checks succeed for that exact head. Drafts, deleted files, foreign authors/forks, changed head, incomplete listings, workflow/source/requirement/license edits and unresolved requested changes are rejected. Native branch rules remain authoritative; this workflow cannot push or merge code.

`workflow_run` automatically rechecks eligibility after the required workflows complete. A maintainer may also manually dispatch it with a PR number. The policy has deterministic rejection tests in `tests/ci/verified_review.test.cjs`. Ordinary code PRs still use required checks and native auto-merge; an automated pass is never evidence that all application acceptance criteria are satisfied.

## Python and runtime dependency maintenance

`requirements.txt` is the single authoritative set of pinned application versions. `requirements.lock` is a backward-compatible `-r requirements.txt` entry point, not another copy. Docker and CI include/cache the authoritative file. Dependency comparisons and the GPU-independence guard follow bounded checkout-local includes, so identical wrappers cannot conceal different pins. `.python-version` records Python 3.12. Docker major-version updates are held for intentional runtime compatibility work; patch/minor updates remain enabled.

`Start-CodexSprint.ps1` now previews the exact effective invocation without starting Codex. `-Run -Resume` resumes the stored goal/verification contract rather than reloading a possibly changed prompt. `-Status` only reads progress. No model loop was launched during verification.
