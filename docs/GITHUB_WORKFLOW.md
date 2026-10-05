# GitHub Issue and Pull Request Workflow

## Verification triggers and merge gate

The `Verification / product-suite-and-connected-ui` check runs the complete suite
for pull requests (opened, reopened, synchronized with new commits, and marked
ready for review), pushes to `main`, pushes to `codex/core-workflow-trial-*`
branches, and manual `workflow_dispatch` runs. Ordinary feature-branch pushes do
not start a second full suite; an open PR's `synchronize` event verifies each
new head. Core-workflow trial branches are a deliberate exception because that
milestone requires both push-event candidate evidence and PR merge-tree evidence.

The pull-request run checks that its tested merge tree equals the exact PR head
tree. That exact-head PR check remains the merge gate, and every full Verification
run still has to produce and pass all nine runtime reports. A `main` or manual run
does not replace the PR check. For core-workflow trial branches, require both the
exact-head push run and the exact-head PR run before merge. Other feature PRs keep
the deduplicated policy.

| Event | Full Verification |
| --- | --- |
| Push to ordinary feature branch | No; verify when its PR is opened or synchronized |
| Push to `codex/core-workflow-trial-*` | Yes; exact candidate source tree |
| PR opened, reopened, ready, or updated | Yes; exact-head tree guard applies |
| Push/merge to `main` | Yes; post-merge integration check |
| Manual `workflow_dispatch` | Yes; supplemental run on the selected ref |

For core-workflow trial acceptance, record the exact PR head SHA, both
push-triggered and PR-triggered run/check identities, the PR tree-equality
result, and all nine passing runtime reports/artifacts for each required run.
For other PRs, record the PR-triggered run/check identity, tree-equality result,
and nine passing runtime reports/artifacts.

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
