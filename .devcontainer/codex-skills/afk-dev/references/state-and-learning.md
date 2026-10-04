# State and learning

`scripts/afk_state.py` uses the repository's `git rev-parse --git-common-dir` as `<common-dir>/afk-dev/`. Non-Git projects use a hash of their canonical root under the user's local AFK-DEV state directory. Portable lessons live separately under `knowledge/`. These files are local operational memory, not product behavior or release authority.

Use `init`, `status`, `goal-set`, `event`, `failure`, `success`, `lesson-add`, `lesson-match`, `lesson-promote`, `guardrail-add`, `queue`, `doctor`, and `export` with `--project`. Commands carrying structured data accept `--json '<object>'`. `goal-set` accepts `--statement` and repeated `--verification` values; outer-loop verification commands should be argv arrays in `goal.json`.

The store writes JSON by flushed unique temporary file and atomic replace. Journals append with flush and lock. A malformed existing record is preserved and reported, not reset. Interrupted runs keep an `inFlight` marker for reconciliation of git, processes, and partial work.

Events retain IDs, UTC time, project/run/iteration, actor, phase, action, outcome, fingerprint, and evidence paths. Do not dump transcripts. Redact text before any write. Fingerprints use stable causal fields such as command family, exit code, check and job ID, exception, source location, category, and tool version; ignore volatile timestamps/PIDs.

For each meaningful failure: record the attempt, seek a matching project lesson, reproduce or discriminate, establish a proven cause or label it a hypothesis, fix, run focused and aggregate verification, then create/update a scoped lesson. A lesson carries the symptom, cause status, ineffective and successful approaches, validation, selectors, confidence, contradictions, and invalidation criteria. A failed reuse demotes it; repeated contradiction quarantines it. Portable promotion requires at least three validated applications in two distinct projects, no unresolved contradiction, and no project-specific identity or secret. Never promote one-project workarounds automatically.

`status` reports named progress and evidence rather than a synthetic percentage. The project worktree is not dirtied by AFK state.
