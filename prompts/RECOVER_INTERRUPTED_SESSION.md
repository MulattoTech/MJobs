> **Current budget policy:** GitHub Actions, hosted tests, bot/cloud-agent reviews
> and extra AI reviewer sessions are disabled by the user. Older hosted-review/CI
> gates and expired deadlines below are historical where they conflict with
> AGENTS.md's active local-only policy. Use explicit local verification; missing
> hosted checks are NOT_RUN, never PASS. Do not restore billing or automation.

# Codex interrupted-session recovery prompt

Read `AGENTS.md`, implementation state, PR queue, current Git status, open GitHub PRs/issues, and recent
commits for active requirement IDs. Determine what is committed, uncommitted, pushed, tested, and untested.
Never discard unrelated/uncommitted work for cleanliness. Re-run the narrowest validation proving the last
checkpoint. If recorded state conflicts with executable evidence, evidence wins and state must be corrected.
Resume the active PR from the smallest safe point, then update state/evidence before changing PRs.
