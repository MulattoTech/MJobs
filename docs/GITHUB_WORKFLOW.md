# GitHub Issue and Pull Request Workflow

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
- main stays green;
- dependency order beats raw parallelism.

Branch: `codex/mj-pr-###-short-slug`.

PR body minimum:
Purpose; Requirements/Closes; Architecture impact; Data/migration impact; Safety/truthfulness impact;
Tests/results; Evidence; Known limitations; Rollback/recovery.

Close an issue only when acceptance behavior is verified or explicitly superseded/not-planned.
A merged PR alone is not PASS evidence.
