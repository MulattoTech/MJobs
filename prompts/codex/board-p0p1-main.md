# MAIN IMPLEMENTATION PROMPT — MJobs P0/P1 Capability Factory

You are the **sole integration owner** for an authorized development campaign on `MulattoTech/MJobs`. This is an **EXECUTE / TEST / INTEGRATE / CONTINUE** mission, not a research report or planning exercise. The adjoining /plan and /goal are already the user's authorization for implementation within the restrictions below. Deliver the most meaningful completed P0/P1 **vertical features and complete capabilities** that real evidence and available time permit.

## Authority and boundaries

- Follow the user's instructions, `AGENTS.md`, the installed MJobs delivery skill, current GitHub Project #1 issues, and their relevant specs.
- **GitHub Project #1 is the exclusive work-selection/ownership/delivery queue**: https://github.com/users/MulattoTech/projects/1. Do not independently select work via GitHub Issues search, an AFK journal, a separate task list, a dashboard, or historical memory. Read an issue only after selecting its Project card.
- **Project Priority** must agree with the issue's `priority:P0`/`priority:P1` label. Inspect and use dynamically discovered Project fields. Select only eligible P0/P1 Project cards. A lower-priority *supporting change* is permitted only if indispensable for completing a selected P0/P1 contract; never silently reprioritize unrelated work.
- **Product acceptance** comes from the issue's original criteria and `planning/CAPABILITY_MATRIX.json` / `planning/REFERENCE_GAP_MATRIX.json`. The Project Acceptance field mirrors proven outcomes. `Done` is a delivery status, not permission to call a broad parent PASS. Do not manipulate denominators or close issues just to make metrics match.
- Preserve current uncommitted files, worktrees, locks, history, backups and other agents' branches. The canonical MJobs root is `C:\Tools\Dev\MJobs`; do not reset it or assume its current checkout equals remote main. Start implementation on fresh, appropriately owned feature worktrees from verified `origin/main`.
- Preserve real career facts and personal data. **No live external job application submission, paid model calls, CAPTCHA/MFA/access-control bypass, anti-bot/rate-limit evasion, credential leaks, host reboot, driver/firmware/network modifications, destructive cleanup or forced shared-history rewrite.**

## First checkpoint: reconcile, rank, claim

1. Verify actual model/effort, current Git `HEAD` and `origin/main`, pending PRs and exact-head CI, running processes and shared ownership claims. Read `AGENTS.md`, `.agents/skills/mjobs-delivery/SKILL.md`, `planning/SESSION_HANDOFF.md`, and the compact requirement index. Read only the affected specs/tests, not giant historical journals.
2. Run `gh project view 1 --owner MulattoTech --format json`, `gh project field-list 1 --owner MulattoTech --format json`, `gh project item-list 1 --owner MulattoTech --limit 1000 --format json`. When available, run `python -m tools.verification.project_board_alignment`; otherwise directly cross-check the relevant issues. Known open+PASS/Status Todo cards require manual evidence inspection, *not* blanket closure or label edits.
3. Build a compact 6–10-card implementation shortlist from unowned P0/P1 items with incomplete acceptance, priority to security, data integrity, meaningful user behavior, dependency readiness and likelihood of verifiable completion. For each: Project item/issue, current state, exact missing behavior, required cross-layer changes, owner/conflict risk, decisive test, potential acceptance improvement.
4. Aim for **3 completed P0 cards, 3 completed P1 cards, and 2 fully usable capabilities**, if achievable. These are ambitious targets, not a minimum quota that justifies fake PASS or tiny paper tasks. Prefer existing cards; create bounded child cards only for genuinely independent deliverables and add them to Project #1 before work. Never count multiple commits to one card as multiple completed features.
5. Claim chosen cards with Project Status `In Progress` using `gh project item-edit` and field/option IDs freshly obtained from the CLI. Read back each update to verify it stuck. If Project writes or safe ownership checks fail, do not claim to own that card; choose other unowned work or stop only that lane.

## Define complete deliverables, not code fragments

A **vertical slice** is a tested, user-observable behavior spanning all layers it requires. A **complete capability** joins related slices into a usable workflow with persistence, error handling and restart/reload behavior. Deliver fewer complete features rather than many disconnected functions.

Select the highest-value relevant contracts dynamically. Examples to *consider only when on the current eligible Project board*:
- P0: actual startup/bootstrap/recovery, bounded external calls and cancellation, category-specific concurrency and admission safety.
- P1: controlled form-analysis → explicit review → DRY_RUN preparation and validation; durable operation processing and restart recovery; meaningful source→job→match provenance; supported application state and end-to-end mock tests.
These examples are not blanket task assignments. A successful fixed-field mock alone does not constitute broad ATS automation; a new unit test alone does not prove the complete user experience. When the board has no P0/P1 cards in `application_automation`, do not pretend it does.

For every selected card, inspect implemented code first. Identify whether the gap is missing functionality, disconnected UI/API/storage, incomplete recovery, real blocker or missing acceptance evidence. Follow established contracts and reuse existing worker queues, schemas, auth and provenance mechanisms. Avoid duplicate frameworks, unrelated refactors and repetitive planning artifacts.

## Parallelism without collisions

Use **one coordinator/integration owner**, up to **three concurrent independent implementation agents**, and spare capacity for focused read-only investigation/review, subject to actual CLI support and ownership checks. Hard cap of six concurrent agent threads. Never spawn overlapping writers or a second integration owner. Separate writable worktrees and exact file/issue scopes; serialize shared migrations, root services, configuration, dashboards and acceptance matrices. If independent work is not safe, run serially.

Implementation agents own end-to-end functional outcomes, not trivial file edits. Each returns a branch, tested behavior, focused evidence, outstanding limits and integration notes. Avoid delegating large meta-audits when coding can begin.

## Per-card implementation and proof loop

1. Specify **observable acceptance**: user's entry point, input, expected response, durable readback, security/privacy boundaries, failure/cancel/retry and restart behavior.
2. Reproduce the missing behavior or write a failing test that would catch the gap. Design only as much as needed, then **implement production code**. Do not hand off for a generic repeat approval when issue acceptance already specifies what to build and this mission authorizes execution.
3. Wire required UI, API/domain logic, persistence/migrations, workers/background processing, browser automation and recovery. Do not force every layer into a task that genuinely needs fewer layers.
4. Run targeted unit tests while editing; then the **affected PostgreSQL/Redis/integration, real headless Chromium and/or synthetic Compose paths** as justified. Verify correct profile isolation, identity, idempotency, stale approval behavior, error classification, cancellation and crash recovery. Never exercise actual external submissions.
5. Analyze unexpected tests for real root causes; never delete, skip or weaken an assertion to conceal a defect. Do not repeat deterministic failed runs without a changed hypothesis/code/environment. Log skipped checks as NOT_RUN.
6. Inspect diff and provenance, commit with actual `AI-Agent`, `AI-Model`, `Requirement`, `Validation`, `UTC-Date` trailers and run commit provenance validation. Open a short-lived reviewable PR linked to the Project issue. Use actual required branch protection and exact-head GitHub checks. No claim of human review if none occurred.
7. Integrate only once required checks are satisfied. Confirm merged revision and affected product behavior. Record sanitized evidence with exact tested SHA and meaningful remaining gaps. Update the proper acceptance matrix **only if every corresponding original criterion is met**. Update Project Status/Acceptance with verified readback.
8. Refresh the short handoff and select the next eligible card immediately. Keep shipping until actual runtime/usage/permission boundaries, a legitimate user stop, verified completion, or circuit-breaker conditions prevent further safe work. A blocked item should not stop unrelated deliverable work.

## Validation economy and release truth

Run focused checks early and broad required checks at integration milestones; avoid running the whole expensive suite after every small change. Use the repository's real commands: `python -m tools.verification.catalogue`, `python -m tools.verification.progress validate`, `python -m tools.verification.progress_dashboard --check`, scoped `pytest`, frontend build/tests, browser/integration checks and `git diff --check`. Use `python -m tools.ci.release_gate --scope p0p1 --online` only when assessing whole P0/P1 release acceptance, and do not weaken its conditions to show progress.

Project cards may be Done without broad parent PASS. Track **delivered issue-backed functional features**, **completed usable capabilities**, and **original accepted P0/P1 requirements** separately. Do not let large umbrellas block real progress, but do not declare them PASS from an isolated slice either.

## Checkpoint and outcome

At every meaningful integrated checkpoint report a compact scoreboard:
- P0 cards implemented/verified: issue IDs, behavior and PR links.
- P1 cards implemented/verified: issue IDs, behavior and PR links.
- Complete user workflows/capabilities available now and how to demonstrate them.
- Exact tested SHAs, meaningful unit/integration/browser/CI results, known NOT_RUN or skipped evidence.
- Original P0/P1 PASS before→after; changes to PARTIAL/BLOCKED if appropriate.
- Next eligible Project cards and ownership conflicts.

Do not report speculative percentage improvements or PR counts as product completeness. Keep handoffs precise and resumable rather than promising an unlimited single session. **Begin now by identifying and claiming the first safe high-impact P0/P1 feature wave, then IMPLEMENT it, verify it, publish it and continue. No planning-only exit.**
