# Codex interrupted-session recovery prompt

Read `AGENTS.md`, implementation state, PR queue, current Git status, open GitHub PRs/issues, and recent
commits for active requirement IDs. Determine what is committed, uncommitted, pushed, tested, and untested.
Never discard unrelated/uncommitted work for cleanliness. Re-run the narrowest validation proving the last
checkpoint. If recorded state conflicts with executable evidence, evidence wins and state must be corrected.
Resume the active PR from the smallest safe point, then update state/evidence before changing PRs.
