# MJobs PR publication policy

`planning/PR_QUEUE.json` policy is authoritative for publication capacity. Its
schema remains version 1, `max_active_prs` remains a positive integer, and historical
`items` retain their original meaning. Default: **32 concurrently OPEN development
PRs**, including drafts. Merged/closed PRs do not count. Draft is a separate state,
not a category or an exemption. Integration stays serialized under one owner;
publication permission does not authorize merging.

`capacity_behavior: warn` reports WARNING when current/projected capacity reaches
32 and allows an otherwise qualified, authorized candidate above it. `block` allows
a candidate bringing the count to 32, but refuses one bringing it above 32. Change
the positive integer or behavior only through reviewed policy. Legacy policies
without `capacity_behavior` retain hard-cap behavior. Neither mode permits empty
PRs, duplicate work, unowned changes or an unlimited publication loop. A warning
requires a backlog/CI/conflict report and an explicit bounded publication scope.

## Read-only preflight and classification

Run from the repository root using cached Python and authenticated GitHub CLI:

```powershell
python -m tools.verification.pr_queue --live
python -m tools.verification.pr_queue --live --metadata <reviewed-metadata.json> --candidate <reviewed-evidence.json>
```

This queries **all pages** of `GET /repos/MulattoTech/MJobs/pulls?state=open` through
`gh api --hostname github.com`. It never publishes, merges, closes PRs, reruns checks
or cancels work. `--snapshot <rest-pulls.json>` is an offline operator input; its
freshness/authenticity is unverified. Requery immediately before publication and
read back after it: this preflight is not an atomic reservation across workers.
One integration owner coordinates simultaneous publishers.

Dependency automation is excluded only for the authenticated GitHub Dependabot
principal: numeric user ID `49699333`, login `dependabot[bot]`, **and** type `Bot`.
These were verified from REST PR #663. GitHub's app principal is the identity even
when `performed_via_github_app` is null. A title, branch, label, bot-looking login,
human publisher, or unverified app name is insufficient. No other bot is excluded
without a separately reviewed trusted identity change. Unknown/missing actors
count as development; malformed snapshot identity/state blocks publication.

Authorship and category are independent of the GitHub publisher and draft state.
Both default to `unknown`, which counts. Never infer AI authorship from a human
publisher or human authorship from GitHub `User`. Categories are `feature`,
`docs-maintenance`, `security`, `acceptance`, `integration`, or `unknown`; all count.
Use an operator-reviewed metadata file keyed by PR number:

```json
{"662":{"head_sha":"<full current head SHA>","reviewed_by":"<reviewer>","review_reference":"<durable review reference>","authorship":"AI","category":"security","work_id":"<owned issue/slice>"}}
```

The reviewer must verify the authorship evidence (such as commit provenance) and
scope. The tool checks the exact head, reviewer/reference presence and allowed
values. It cannot authenticate a claim inside an arbitrary JSON file: review the
file through the normal ownership/review process; PR-authored bodies/labels are
not trusted reviewed metadata. Stale, missing or malformed metadata becomes
unknown. Do not edit historical queue items merely to classify live PRs.

Candidate evidence has `head_sha` (full SHA), `head_ref`, `work_id`, `owner`, positive
integer `commit_count` and `changed_files`, `meaningful_change: true`, a nonempty
`local_validation` list of executed command/results, and `nonduplicate_review`
describing the live scope comparison. The tool rejects identical open head SHA,
branch or reviewed work ID; semantic duplication still requires human/owner review.
These are reviewable attestations, not proof of executed tests or meaningfulness.
Verify the actual diff/commits and preserve safe rollback before publication.

The report includes counted backlog numbers, development drafts, authorship and
category totals, and known/unknown conflict state. REST list responses often omit
`mergeable`; unknown is never clear. CI is explicitly `NOT_QUERIED`: inspect exact
head checks separately (`gh pr checks <number>` / `gh pr view <number> --json
headRefOid,statusCheckRollup,mergeable`). Never treat missing, queued, stale or
skipped checks as PASS. A report without a candidate is inventory only;
`publication_checked` distinguishes it from publication preflight.

## Publication and CI execution are separate

Audit at baseline `06ffb07a207dc68b42b565870f67fc3eb67aae03`: Actions enabled;
no repository self-hosted runners returned. Hosted runners use ubuntu-24.04.
Fast, integration, synthetic acceptance, dependency/security and other workflows
use separate workflow/PR concurrency groups. Superseded runs may cancel only
within their existing group; no cross-PR cancellation is introduced. Browser
regression has matrix `max-parallel: 4`; integration has `max-parallel: 3`.
These limits apply **per matrix/run**, not globally. Non-matrix jobs do not support
`strategy.max-parallel`; workflow concurrency provides a single running group,
not a 4–6-slot semaphore. Account/organization runner quotas are external controls.

Aim for 4–6 simultaneous expensive jobs where supported by existing matrices and
operator scheduling. This policy does not claim to enforce that aggregate target.
Keep heavy manual workflows bounded and stagger their authorized dispatches;
publishing 32 PRs may queue many required checks. No billing/quota changes, new
runner/services, check skipping, unrelated cancellation, or security/required-gate
weakening is authorized. Existing pip/npm caches are reused; this PR adds
`requirements.lock` to the fast Python cache key while retaining `requirements.txt`
and test requirements. The lockfile includes `requirements.txt`, the authoritative
pin set; both wrapper and pins now invalidate that cache. Other pip caches already
key the authoritative pins. No scheduling change is justified by this audit.

Main is protected by active history safety and verified integration rulesets
(legacy branch-protection endpoint returns 404). Baseline requires CI / required,
Integration / PostgreSQL and Redis, Disposable Compose / mock end-to-end,
Dependencies / introduced vulnerabilities, Security / secret scan, strict checks,
thread resolution and no bypass actors. Preserve these rules. Native/local review
does not substitute for required checks or authorize integration.

## Consumers and verification

`tools.verification.catalogue` now validates the policy shape as well as historical
coverage/dependencies. `tools.verification.pr_queue` consumes live capacity and
candidate gates. `tools.coordination.lane_coordination` copies legacy queue files;
it does not enforce capacity. Agent skill manifest/validator and work-history
classification reference the path, not its numerical cap. Historical plans and
evidence remain historical. No old runtime capacity consumer was found beyond
the policy JSON; workers must use this preflight instead of raw PR counts or title
queries. CI's existing unit/catalogue jobs execute the regressions/shape validation;
live admission stays with the authorized owner, not an elevated PR workflow.

```powershell
python -m pytest -q tests/unit/test_pr_queue.py tests/unit/test_catalogue.py tests/unit/test_agent_skills.py
python -m tools.verification.catalogue
python -m tools.verification.agent_skills
git diff --check
```
