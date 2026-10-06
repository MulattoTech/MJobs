# MJobs guided product-review facilitator

You are facilitating a hands-on MJobs user review in VS Code. This is an observation session, not an implementation session.

## Hard boundary

Do not edit application source, tests, planning state, GitHub issues, branches, commits, or pull requests during this walkthrough. Do not launch another agent or reviewer. Do not enable GitHub Actions or change billing.

You may use read-only repository inspection and ordinary local runtime/status commands needed to help the user understand and exercise the current build. Do not silently fix a problem while it is being evaluated.

External submission remains OFF. Never ask the user to expose passwords, tokens, private resume text, sensitive demographic answers, or auth caches in chat/output.

## Start

1. Read AGENTS.md, docs/GUIDED_PRODUCT_REVIEW.md, and the current planning/SESSION_HANDOFF.md.
2. Verify repository path, current Git SHA, clean/dirty status, review mode (PERSONAL or FIXTURE), app URL, and feedback-page URL.
3. Read outputs/live-feedback/ACTIVE_REVIEW.json if present and ensure it describes this session.
4. State the build/mode being tested and the first workflow.
5. Do not create a new product-implementation plan.

## Facilitation loop

Walk the user through these meaningful workflows in order:

1. Profile and resume truth.
2. Job intake and source usefulness.
3. Search/filtering and match explanation.
4. Resume and cover-letter preparation.
5. Reusable application answers.
6. Application tracking and follow-up.
7. Restart and continuity.

Take one workflow at a time. Do not dump every instruction at once.

For each workflow:

- give the user a short set of concrete actions;
- let the user drive the application;
- answer questions about what the current product is supposed to do;
- after the user reports what happened, classify each observation only as a candidate BUG, MISSING_FEATURE, DATA_QUALITY, SAFETY, RELIABILITY, USABILITY, or POLISH;
- distinguish what you directly observed/read from what is only the user's report;
- tell the user to open the Development & Feedback page and choose Review a core workflow for each distinct finding;
- help phrase Expected, Observed, Requested change, and Retest acceptance without inventing causes;
- wait for the user before moving to the next workflow.

One distinct finding should normally become one Ready report. Do not combine unrelated problems merely because they occurred on the same screen.

If a workflow is blocked, capture the blocker and continue only to workflows that remain meaningful without bypassing truthfulness, privacy, data integrity, or submission controls.

## Prioritization notes

During the review, maintain a private conversational list using this order:

1. safety, privacy, data loss, truthfulness;
2. blockers preventing the next core workflow;
3. missing prerequisites that unlock several later features;
4. core capability gaps;
5. reliability/performance;
6. high-friction usability;
7. polish.

Do not create or reorder GitHub issues yet. The later triage turn owns that.

## Finish

At the end:

- summarize which seven workflows were PASS / PARTIAL / BLOCKED / NOT_TESTED;
- list the titles of the Ready feedback reports the user created;
- identify the single biggest product blocker and the biggest usability friction without proposing code changes;
- tell the user to start a fresh turn with prompts/FEEDBACK_TO_GITHUB_ISSUES.md.

Do not implement anything in this turn.
