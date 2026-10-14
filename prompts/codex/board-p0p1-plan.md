/plan

MJOBS — PROJECT #1 MULTI-CAPABILITY DELIVERY PLAN

This is the *planning section of an authorized implementation run*, not a request to stop after producing a plan.

Repository: MulattoTech/MJobs
Work queue: https://github.com/users/MulattoTech/projects/1
Priority scope: P0 and P1 only, with narrowly necessary dependency code allowed.

1. PREFLIGHT (bounded)
Read AGENTS.md, .agents/skills/mjobs-delivery/SKILL.md, planning/SESSION_HANDOFF.md, planning/REQUIREMENT_INDEX.json, and current Git state. Fetch origin/main and verify existing worktrees, pending PRs, ownership claims, real running agents and required CI. Preserve all dirty/unmerged work and existing personal data.

Retrieve GitHub Project #1 with `gh project view 1 --owner MulattoTech --format json`, `gh project field-list 1 --owner MulattoTech --format json` and `gh project item-list 1 --owner MulattoTech --limit 1000 --format json`. Check issue membership, one P0/P1 label consistent with the Project Priority, and open/closed state consistent with delivery Status. Run `python -m tools.verification.project_board_alignment` when that tool is installed in this checkout; otherwise verify equivalent fields directly with the authenticated CLI. Do not infer task completion from Project Status or automatically close open issues whose Acceptance is PASS.

2. RANK REAL IMPLEMENTATION WORK
Select exclusively unowned Project cards with Priority P0/P1 and unmet *original* acceptance criteria. Read each selected issue, its area spec and relevant source/tests to distinguish missing implementation, disconnected components, missing live evidence, and an already-finished feature. Do not independently select P2–P6, change priorities, or choose work from the AFK journal or a separate issue queue. A lower-priority dependency may be touched only when essential to the chosen P0/P1 card.

Favor deliverable *capabilities* composed of genuinely working vertical slices over tiny low-value tickets. Candidate families to assess against current board and code: P0 bounded operations, concurrency and startup; P1 controlled DRY_RUN, application/operation recovery, persistent pipeline, discovery and connected tests. These are examples, not preselected assignments. Prioritize severe security and data-loss defects; then end-to-end user value, dependency readiness and achievable acceptance.

3. DESIGN AN EXECUTABLE DELIVERY WAVE
Produce a compact table of the best 6–10 cards showing: issue and Project card IDs, Priority, current Acceptance, exact missing user behavior, blocking dependencies, existing PR/claim, file ownership, end-to-end acceptance test and potential for original PASS. Target **at least 3 completed P0 and 3 completed P1 implementation cards**, plus **at least 2 complete user-usable capabilities**, subject to actual feasibility. Never subdivide artificially to inflate the count.

Group non-overlapping cards into at most three simultaneously writable workstreams; the root agent is the single integration owner. Plan a short PR/CI/acceptance checkpoint for each completed feature. Choose a first wave of ready cards and their assigned paths, not another sprawling roadmap.

4. EXECUTE IMMEDIATELY
After that concise plan, proceed directly to source changes, tests, PRs and successive cards under the accompanying main directive. Do not end the session with only this plan. Do not ask again for permission already conveyed by the implementation prompt, while respecting any actual repository/safety gate.
