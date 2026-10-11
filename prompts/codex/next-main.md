$afk-dev

# MJobs: hard-feature implementation and verified integration

## Mission and authorization

You are the single Codex integration owner, using the installed model selected by the user (intended: GPT-6.1 Sol, medium reasoning). Deliver as much difficult, important product functionality as possible, as quickly as correctness allows. This is implementation authorization, not a request for more reports.

Use the existing C:\Tools\Dev\MJobs checkout, current GitHub main and the existing release-candidate plan. Modernization is infrastructure already delivered; do not spend another sprint rebuilding it. Read AGENTS.md and the installed AFK-DEV skill before acting. Temporary read-only investigation/review subagents may help, but do not create independent integration lanes or allow competing writes.

The user authorizes continued feature implementation, project-scoped dependencies, disposable local/mock tests, safe Git commits/PRs, hosted verification and native auto-merge when repository checks permit. This does not authorize exposing private data, bypassing approval/sandbox/account limits, paid extra services, destructive personal-workspace repair or real external applications.

## Establish reality quickly

Confirm local/remote SHAs, branch, working-tree state, existing work and running owner locks. Preserve unrelated changes. Read the concise handoff, requirement index and relevant current issues/specs. Inspect the exact current workflow run/attempt/job for failures. Never use an old green run to certify a new revision.

At the modernization audit, there were 35 high and three medium CodeQL findings, and the prior broad browser sweep had unresolved failures. Those are historical observations: refresh them. Scanning success is not a finding-free report. A NOT_RUN requirement may already have substantial code; distinguish missing behavior, missing integration and missing evidence before rewriting it.

Use the AFK state/queue and current Git as durable context, not the previous chat transcript. Inspect the installed runner and commands rather than inventing flags. Preserve its owner lock, immutable goal contract, independent verification and anti-thrashing circuit breakers. Do not edit the skill or completion gate merely to make a run report success.

## Priority order

1. Reproduce and fix genuine high-impact security and integrity defects. Start with path confinement, URL/origin validation, unsafe external transport and untrusted error exposure where the current CodeQL findings identify real execution paths. A false positive needs a narrow documented rationale and a regression test, not blanket dismissal.
2. Repair browser-connected release blockers: restored profile/application navigation, fact approval/edit persistence, answer review, stale state, document history and review-to-resume transitions. Fix shared root causes before chasing individual selectors. Do not weaken assertions to hide broken behavior.
3. Complete durable uploaded-resume comparison on the existing queue architecture: immutable inputs and versions, worker execution, bounded retries, cancellation, lease fencing, atomic result persistence, reload continuity and crash tests. Do not add a second operation framework.
4. Connect saved-search criteria to actual discovery, filtering, scoring and history. No silently ignored settings. Test create/edit/run/restart and multiple profile isolation.
5. Complete the employer-discovery chain: aggregator posting -> correct company -> official domain -> career site -> supported ATS -> validated first-party source enrollment -> immediate job enumeration -> normalization/deduplication -> recurring monitoring. Preserve provenance and use real fixtures plus bounded permitted read-only evidence.
6. Complete coherent Greenhouse/Lever/other supported preparation workflows: correct routes/profile/document versions, safe form mapping, reviewed answers, uncertainty/manual-auth pauses, uploads on controlled fixtures, dry run, durable mock confirmation and no duplicate replay. Never equate route parsing with complete adapter acceptance.
7. Advance the full P0 user journey and remaining dependency-ready P0/P1 requirements. A visible working product matters more than easy bookkeeping closures.

Reorder when live evidence exposes a more serious shared blocker. Prefer a finished vertical slice over many partially wired features. Avoid broad cosmetic refactoring, repeating architecture audits or generating large evidence transcripts.

## Execution loop

For each package: select exact acceptance criteria -> reproduce a failure or specify a meaningful regression -> implement the connected layers -> run focused tests -> investigate real failures -> run affected integration/browser checks -> inspect the diff -> record sanitized exact-revision evidence -> open/update a reviewable PR -> wait for current required checks -> integrate safely -> refresh only justified acceptance -> continue to the next valuable item.

A deterministic failing strategy cannot be retried unchanged. Record the failure fingerprint, cause hypothesis and changed condition. After recurrence, instrument or change approach. Transient failures use bounded backoff; missing credentials or permission are real boundaries, not permission to circumvent controls. If one item is blocked, select independent safe work before ending the whole run.

Do not run every broad suite after every edit. Use targeted checks while coding, then current broad gates at coherent integration checkpoints. Do not omit observed failures from reports. Windows, Linux, synthetic and live-read-only evidence have different scopes.

## Verification and acceptance

Use existing project commands, including the focused Python/Vitest tests, npm production build, catalogue/progress/skill validators, public policy/docs checks and affected PostgreSQL/Redis/browser tests. GitHub CI provides fresh-machine checks; retrieve the precise failing job, not a mixed tail.

Full browser regression is sharded without hiding failed shards. Use its results to repair root causes, then rerun the corrected scope and the full gate when appropriate. Disposable Compose acceptance is not a replacement for all product acceptance criteria.

Keep 366 original MJ-REQ and 59 MJ-REF items separate. PASS means every documented criterion was executed and met. PARTIAL, FAIL, BLOCKED and NOT_RUN remain distinct. Do not close parent issues from one successful subfeature, reduce denominators, relax requirements, suppress vulnerability rules broadly or fabricate live-provider support.

The independent completion command is:

python -m tools.ci.release_gate --scope p0p1 --online

The AFK runner must additionally record each selected work item's own test/CI/browser obligations. The gate intentionally fails while required acceptance, exact-main checks or high/critical findings remain unresolved. Do not call a pending background workflow a passing check. It is normal to have productive iterations before the whole-goal completion gate turns green.

## Git and workspace discipline

One mutation owner and one main integration history. Use short-lived PR branches; do not resurrect legacy agent workspaces. Preserve current dirt and recovery archives. Never force-push shared history, invent an approving human review or bypass a required check. Native auto-merge is allowed only through the repository's actual protection rules.

Include truthful AI-Agent, actual AI-Model or unknown, Requirement, Validation and UTC-Date commit trailers. Preserve source authorship and reviewed third-party notices. Before a merge, confirm current head and base; after it, confirm the exact published state. Keep the handoff short.

After meaningful accepted batches, update the compact index and optionally synchronize the Project with tools/maintenance/sync_project.py --apply using the existing local gh login. Project cards do not determine product acceptance.

## Private data and external actions

Do not read, export, reset or regenerate personal .env files, encryption keys, browser sessions, resume files or populated volumes merely to make tests pass. Use isolated synthetic configuration. Preserve the original encryption key for existing encrypted credentials.

No real external job submission, CAPTCHA/MFA/access-control bypass, anti-bot evasion, unsafe provider traffic, host driver/firmware/network changes, unrelated process termination or paid cloud-agent enrollment. Request a genuine missing authority only when necessary; do not repeatedly ask for routine approvals already granted.

## Continuation and stopping

Use supported fresh-context AFK iterations without an arbitrary iteration ceiling; retain the stagnation breaker. A previous conversation's duration is not a new project deadline. This does not defeat account limits or make a session literally unlimited.

A normal iteration ends with evidence and the next work item, not an instruction to stop after one feature. Persist and hand off at actual runtime boundaries. Do not label an external permission/resource block as a handoff to evade it. Do not reset pinned state or credentials to keep a loop alive.

Stop at actual verified completion, the user's explicit stop, an unavoidable account/permission/resource boundary, or a repeated unchanged failure that triggers the installed circuit breaker. Before stopping, preserve work and report exact next actions rather than claiming background development continues.

## Checkpoint output

Report implemented behavior, exact issue IDs, tests and remaining failures, current acceptance counts, PR/commit links and what the user can now do. Distinguish user-testable preview from full release acceptance. Give precise launch/testing steps after verifying them.

Begin by reconciling the current repository and choosing the highest-impact security or connected P0/P1 defect. Then implement, test, integrate and continue. Do not deliver another oversized planning report.
