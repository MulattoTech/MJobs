# GitHub issue and pull-request workflow — local-only

GitHub Actions is disabled at repository level and each known workflow is manually
disabled. Checked-in workflow jobs also have a literal false guard and no push/PR
triggers. Keep them for reference, not execution. No new hosted run/review is allowed.
No repository rulesets or webhooks were found at cutover; account-level external-app
review settings are not exposed by that read and must not be assumed disabled.

## Local acceptance replaces hosted merge gates

The user's 2026-10-05 instruction supersedes earlier requirements for independent AI
review and exact-head push/PR Actions. Do not retry a billing-blocked job. Read old
logs only as historical evidence. Do not invoke reviewer agents anywhere.

Use one local writer, explicit focused commands, and synthetic temporary data. Build
before browser checks. Require actual non-skipped acceptance and negative controls.
Preserve immutable history, profile boundaries and external-submission OFF. Never
relabel unavailable Docker/PostgreSQL tests as SQLite coverage or broad release PASS.

Record exact source SHA/tree or input hashes, base, command, UTC time, platform,
counts, result and limitations under docs/evidence. Raw CLI transcripts are private.
Run existing catalogue/progress/skill checks when their inputs change. Documentation
edits after a product test require unchanged executable-input proof and metadata
checks; changes to executable inputs need new affected local evidence.

## Publication

Commit only intended owned files. Push each coherent checkpoint to the working
branch and verify the remote SHA. WIP may remain on a published draft branch, never
unverified main. Do not force-push, bypass protection, or create fake green checks.

After local acceptance, use a PR only when it will not request external agents.
When that cannot be established, an ordinary non-force fast-forward push to main
is authorized if current main is an ancestor of the exact candidate. Recheck current
remote main immediately before pushing; reject divergence. Verify remote main after
integration and reconcile the primary checkout only with confirmed idle ownership.
Document the commit and any existing PR's integrated state without claiming hosted
or independent-review acceptance. Close no broader requirement by implication.

No new budget authorization is implied. No Actions, automatic review, cloud test,
self-hosted GitHub runner, provider spend, private-data transfer or deployment.

## Issue and queue conventions

`planning/ISSUE_SEED.jsonl` is the machine-readable source for backlog creation.
Every issue has `<!-- mj-requirement:ID -->`; sync by marker, never mutable title.

The catalogue contains one issue for every numbered requirement plus independently shippable integration
children. `planning/GITHUB_ISSUE_MAP.json` records IDs to GitHub issue numbers after synchronization.

`planning/PR_QUEUE.json` is a plan. Statuses:
`QUEUED`, `BLOCKED`, `ACTIVE`, `DRAFT_PR`, `READY`, `MERGED`, `SUPERSEDED`.

Rules:
- no empty PRs;
- max 4 active by default;
- one coherent reason per PR;
- link requirement IDs and GitHub issues;
- include implementation + required tests/migrations/docs;
- report commands actually run and results;
- main stays locally verified; hosted checks remain disabled;
- dependency order beats raw parallelism.

Branch: `codex/mj-pr-###-short-slug`.

PR body minimum:
Purpose; Requirements/Closes; Architecture impact; Data/migration impact; Safety/truthfulness impact;
Tests/results; Evidence; Known limitations; Rollback/recovery.

Close an issue only when acceptance behavior is verified or explicitly superseded/not-planned.
A merged PR alone is not PASS evidence.
