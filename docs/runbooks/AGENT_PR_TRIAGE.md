# Agentic PR backlog triage

A small GitHub-hosted workflow, `.github/workflows/agentic-pr-triage.yml`, classifies
open pull requests four times daily, or when an authorized operator dispatches
the workflow. It does **not** use privileged pull-request events. It uses the repository's own GitHub Actions token, not paid
Copilot credits and **not a runner on MulattoTechBox**.

## What happens automatically

- Applies exactly one `agent-triage:*` classification: `ready`, `running`,
  `failed`, `behind`, `conflict`, `stale`, `draft`, or `review`.
- For a trusted, author-owned PR updated within 30 days, with **all observed
  checks green** and `mergeable_state=clean`, enrolls it in native squash
  auto-merge if it has `agent:automerge`. Small, low-risk Markdown-only
  changes under `docs/` can qualify without the opt-in label.
- Refreshes **at most one** stale-base branch per run when explicitly opted
  into `agent:automerge`, allowing its checks to run against current main.
- Caps newly enabled auto-merges at one per run, avoiding a large CI storm.
  Existing GitHub branch rules, review requirements and checks remain final
  authorities. The workflow never merges directly or bypasses protections.

## Agent protocol

After an independent scoped review, code checks, and issue/requirements
reconciliation, the integration owner can apply `agent:automerge` to an
approved PR. This can be done by an agent with its existing permitted issue-label
workflow; it does **not** require the user to manually click Merge.
For risky or incomplete changes, mark `agent:hold`, which vetoes auto-merge.

If a PR is blocked by conflicts, failed checks, missing acceptance or an
unmerged parent, **fix or rebase the PR before adding the merge label**.
The triage workflow reports the blocker but does not invent a resolution.
PRs with stale commits (30+ days) need a new reviewed commit before automatic
admission. No automatic stale PR closure or branch deletion happens here.

The workflow never checks out untrusted PR code in a privileged context.
Only checked-in default-branch triage source executes. It does not modify
secrets, repository rules, projects, production data, or local Docker instances.

## Runtime/cost controls

- One hosted job per trigger, bounded to 8 minutes.
- Four scheduled runs daily for high-volume MJobs; daily for other active
  repositories, and weekly for lower-activity ones.
- No third-party paid AI reviewer and no repeating full CI unless an opted-in
  stale branch is refreshed.
- GitHub Actions minutes can still accrue, particularly on private repos.
- If repository settings disable Actions write tokens, auto-merge or labels,
  the workflow reports that limitation; it never elevates permissions.

## Routine dependency updates

The MJobs `.github/dependabot.yml` had all five routine update limits at
zero, and was removed at the user's request. Deleting this config does not
by itself switch off separately enabled Dependabot security alerts or
security-update PRs. Keep dependency review and secret scans required.

Other repositories retain their own dependency configuration unless
explicitly changed or reviewed.
