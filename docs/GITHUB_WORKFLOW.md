# GitHub Issue and Pull Request Workflow

## Verification triggers and merge gate

The `Verification / product-suite-and-connected-ui` check runs the complete suite
for pull requests (opened, reopened, synchronized with new commits, and marked
ready for review), pushes to `main`, and manual `workflow_dispatch` runs. Pushes
to feature branches do not start a second full suite; an open PR's `synchronize`
event is the verification for each new feature-branch head.

The pull-request run checks that its tested merge tree equals the exact PR head
tree. That exact-head PR check remains the merge gate, and every full Verification
run still has to produce and pass all nine runtime reports. A `main` or manual run
does not replace the PR check. This keeps the full validation and required check
name while avoiding the duplicate push-triggered suite for the same PR commit.

| Event | Full Verification |
| --- | --- |
| Push to feature branch | No; verify when its PR is opened or synchronized |
| PR opened, reopened, ready, or updated | Yes; exact-head tree guard applies |
| Push/merge to `main` | Yes; post-merge integration check |
| Manual `workflow_dispatch` | Yes; supplemental run on the selected ref |

For PR acceptance, record the exact PR head SHA, the PR-triggered run/check
identity, the tree-equality result, and all nine passing runtime reports/artifacts.
Earlier push-plus-PR receipts remain historical evidence; they are no longer a
requirement for a new PR.

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
