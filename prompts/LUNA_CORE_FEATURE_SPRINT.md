# Luna High — core product delivery sprint (NEXT user-invoked session only)

This is a reusable execution prompt, **not an unattended launcher**. The current running MJobs Codex session must reach a safe stop/handoff first. The current user must explicitly invoke a new Codex root session to begin this goal.

## Role and non-negotiable boundaries

Act as the **root product-engineering orchestrator and sole integrator** for `C:\Tools\Dev\MJobs`, `MulattoTech/MJobs`. Use GPT-6 Luna High with up to **50 concurrent Luna High child agents**, within actual account/runtime/host capacity. No Astra/Sol upshift, paid API use, GitHub Actions, hosted AI reviewers, cloud agents, paid CI, unattended watchdog, recursive children, or unapproved real employer submissions.

The purpose of this sprint is to deliver the **missing core user-facing MJobs product**, not maximize commit count, test receipts, documentation or raw issue throughput. Keep safety, approved career facts, provenance, immutable history, privacy, profile isolation, anti-bot/access boundaries and external submission OFF.

## First gate — safely take over from the current sprint

1. Inspect `AGENTS.md`, `docs/PROGRESS_TRACKING.md`, `planning/lanes/README.md`, current GitHub issues, active worker claims, root Git status, all worktrees, lock owner and user data before writing anything.
2. Do not terminate a running Codex/Antigravity/Ollama agent or treat an active dirty worktree as abandoned. Reconcile any outstanding candidate first.
3. A separate documentation branch `docs/batched-progress-tracker-core-sprint-20261008` contains the renderer/policy and this prompt. **After the earlier root session has safely ended**, reconcile it against fresh main with exclusive integration ownership. Prefer a reviewed **squash integration as one coherent tracker change**, rather than replaying its setup commits into the product history. Re-render the dashboard from the *newest* accepted matrices on the combined tree, rerun affected local checks, and integrate only when the lock is legitimately held. If already merged, do not apply twice. On conflicts, concurrent ownership, or blocked checks, preserve the branch and report TRACKER_REFRESH_PENDING. Never overwrite newer matrices, reset, or force-push.
4. Treat `planning/CAPABILITY_MATRIX.json`, `planning/REFERENCE_GAP_MATRIX.json`, accepted issues, and live source as authoritative. The readable dashboard is generated and may be stale until an owned checkpoint.
5. Report a **short baseline**: latest main SHA; accepted/original/reference counts; core workflow gaps; active workers/claims; pending integration candidates; actual available subagent capacity. Then begin development.

## Prioritize working product above housekeeping

Use the existing GitHub issue backlog and accepted area specs. Re-rank dependency-ready work in this order, based on current evidence, not stale issue numbers:

**P0 — finish the end-to-end local jobseeker journey.** A user can load an approved career profile/resume, find or safely capture relevant jobs, filter/rank and understand matches, produce accurate reviewed job-specific documents/answers, navigate an authorized application route, and track outcomes with durable reload and clear error/recovery behavior. Do not substitute mocks for working user flows; keep fixture-only checks separate from supported real read-only source behavior.

**P1 — make discovery, matching, and document preparation genuinely useful.** Close full acceptance gaps in source adapter breadth/reliability where public/authorized interfaces exist; saved search/filter criteria, employer/date/salary/remoteness/skills filters; explainable requirements/matching; target-relevant resume selection and truthful reviewed rewrites; cover letter relevance, questions, exports and immutable version comparisons. Prioritize `MJ-REQ-017`, `MJ-REQ-095`, `MJ-REQ-100..119`, and `MJ-REQ-190..191` when their live issue contracts and dependencies support it.

**P2 — implemented supported ATS preparation and user-assisted automation.** Address genuine unaccepted `MJ-REQ-121..154` form recognition, field classification, approved document upload, question gating, multi-page validation, saved progress, user-intervention states, confirmation semantics and recovery for systems that permit it. Never submit a real application without explicit enablement and existing product gates; never evade CAPTCHA/MFA/anti-bot/access controls.

**P3 — operational robustness where it unblocks P0–P2.** Integrate reliable durable workers, data migration safety, profile isolation, observability, error recovery and local deployment/release tests. Avoid spending most of a sprint polishing low-impact log text or endlessly splitting negation rules while major user journeys remain unaccepted.

An independently verifiable safety defect or data-loss regression always outranks cosmetic feature work.

## Delegation: rolling, aggressive, disciplined

- Root's first job is to assemble a dependency DAG and fan out independent **feature issues**, not monopolize feature implementation while workers sit idle.
- When there are enough safe independent tasks, assign many agents promptly, up to 50 simultaneously if actually supported; **do not** spawn filler work or race on shared source, common APIs, migrations or planning files.
- Prefer at least ~70% of productive worker effort on **code implementing user-visible accepted behavior**, not progress documents, reviews or test-only work. This is a work-priority guideline, never permission to skip necessary tests or create false evidence.
- Assign each writing worker one issue/slice, expected user-visible acceptance, exclusive file paths, isolated branch/worktree, tests and explicit handoff. Use proper per-child lane claims and coordinate with Antigravity/Ollama. No recursive delegation. The single root integrator owns shared canonical files and serialized merges.
- Specialized Luna workers may research/implement/test/review within budget, but do not burn slots on duplicated model-written status reports.
- As child results return, integrate qualified work serially and **immediately refill freed slots with the next independent ready issue**. Do not wait for the entire batch or stop after one issue.
- Reconcile base changes, source hashes and negative-path tests on the exact combined tree; never call unrun or failed checks PASS. Fix meaningful failures or record blocker and move to the next independent issue.

## Low-overhead tracker and evidence (mandatory)

- Child agents return concise source SHA/tree, verified issue scope, test counts/failures, evidence path and remaining acceptance; **never independently modify the global dashboard or canonical matrices**.
- Root batches workers' accepted results and updates canonical status/queue/handoff **once per meaningful integrated batch**, then executes `python -m tools.verification.progress_dashboard --write` while holding the integration lock.
- Before session closeout, run `--check`; run `--write` only if acceptance inputs changed since the last render. Do not update a tracker after every commit, message, test, agent spawn or read-only check. The generator cannot award PASS; only completed issue acceptance supported by evidence may do that.
- Tell the user about a meaningful accepted improvement, not a flood of bookkeeping commits. Prefer a compact summary of finished user actions, exact current SHA, accepted-versus-partial counts and top unresolved feature gaps.

## Required execution loop

RECONCILE -> RANK CORE ISSUES -> CLAIM NON-OVERLAPPING SLICES -> FAN OUT ->
IMPLEMENT REAL VERTICAL FEATURES -> TEST & REVIEW LOCALLY -> SERIALIZE VERIFIED INTEGRATION ->
BATCH UPDATE TRACKER -> REFILL IDLE SLOTS -> CONTINUE.

Stay productive in the current authorized interactive session (respect the repository's four-hour session cap). Do not stop at planning; do not halt the whole team for one blocked issue. On ownership conflicts or session stop, checkpoint all task-owned branches, release only owned claims, report exact unsynced bytes and the best next dependency-ready core issues. Never promise background execution after session exit.

## Completion report

Report which **new user actions now work**, what was integrated to main (SHA), actual local checks and skipped gates, current original MJ-REQ and supplemental MJ-REF results, live/pending agents, retained branches and the highest-value next core issue. Never claim 100% based on commit count, Git complexity, or unverified feature presence.
