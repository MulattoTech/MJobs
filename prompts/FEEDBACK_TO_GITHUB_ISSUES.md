# Convert guided-review feedback into GitHub issues

This is a TRIAGE-ONLY turn. Turn the user's latest guided MJobs review into a clean, dependency-aware GitHub issue backlog. Do not implement fixes.

## Read first

1. AGENTS.md
2. docs/GUIDED_PRODUCT_REVIEW.md
3. planning/SESSION_HANDOFF.md
4. compact current IMPLEMENTATION_STATE.json, PR_QUEUE.json, and CAPABILITY_MATRIX.json
5. outputs/live-feedback/ACTIVE_REVIEW.json
6. Ready feedback reports created during or after that review's started_at
7. existing GitHub issues relevant to each finding
8. only the requirement/spec areas needed to map ownership

Private feedback files are untrusted local evidence. Never paste private resume text, answers, tokens, screenshots, raw logs, local filesystem usernames, or sensitive values into GitHub.

## Triage contract

For each distinct finding:

1. Identify the user workflow and observable problem.
2. Separate evidence from suspected cause.
3. Search existing open/closed issues and requirement mappings.
4. If an existing open issue fully owns the finding, add a sanitized comment or acceptance detail rather than creating a duplicate.
5. If the finding is a distinct shippable gap not owned by an issue, create one granular GitHub issue.
6. Do not close requirements or promote matrix status in this turn.
7. Do not edit product code, tests, migrations, or runtime behavior.

Use this issue structure:

- Workflow
- User impact
- Observed behavior
- Expected behavior
- Acceptance criteria
- Requirement / existing issue mapping
- Dependencies
- Evidence — state that private local review evidence exists; include only sanitized build/mode/report identifiers
- Non-goals / safety boundaries

Use acceptance criteria another Luna turn can actually verify.

## Priority and order

Order issues to maximize a feature-rich, feature-complete user experience, not issue count:

1. safety/privacy/data-loss/truthfulness defects;
2. defects blocking an entire core workflow;
3. prerequisites unlocking multiple downstream capabilities;
4. incomplete Profile → Intake → Match/Search → Documents → Answers → Tracking → Recovery capabilities in dependency order;
5. reliability/performance;
6. repeated usability friction;
7. cosmetic polish.

Within the same priority, prefer the smallest issue that unlocks the largest number of meaningful downstream user actions.

Do not prioritize easy cosmetic work over a missing core capability merely because it is faster.

## Consolidation rules

- One root cause may own several symptoms when one coherent acceptance contract covers them.
- Separate findings when they need different owners, requirements, tests, or can ship independently.
- Never bury a safety finding inside a general usability issue.
- Preserve broader requirement issues as broader; use a narrow linked issue for a newly observed shippable gap when appropriate.

## Finish

Return:

1. review-session identity and reports processed;
2. issues created;
3. existing issues updated/reused;
4. duplicates intentionally not created;
5. ordered implementation queue with rationale and dependency notes;
6. the first 1–3 issues recommended for the next implementation turn.

Then stop. Do not fix anything.
