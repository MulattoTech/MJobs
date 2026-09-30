# Codex continuation prompt

Continue MJobs from current evidence-backed state.
Read `AGENTS.md`, implementation state, issue map if present, and PR queue.
Reconcile local worktree/branch with GitHub PRs; preserve all existing work.
Finish an incomplete ACTIVE/DRAFT PR before new work when safe.
Otherwise choose the highest-priority dependency-unblocked `QUEUED` PR.
Read only its issues, matching area spec, relevant `AREA.md`, and touched code/tests.
Implement the smallest coherent vertical slice; run targeted tests; fix; run affected integration/E2E.
Update state, queue, evidence, commits, and draft PR.
Keep at most four active PRs and avoid overlapping ownership.
Continue to the next unblocked item without asking for ordinary engineering preferences.
Never perform a real external job submission without explicit user enablement and product gates.
