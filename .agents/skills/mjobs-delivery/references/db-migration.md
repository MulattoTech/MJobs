> **Current budget policy:** GitHub Actions, hosted tests, bot/cloud-agent reviews
> and extra AI reviewer sessions are disabled by the user. Older hosted-review/CI
> gates and expired deadlines below are historical where they conflict with
> AGENTS.md's active local-only policy. Use explicit local verification; missing
> hosted checks are NOT_RUN, never PASS. Do not restore billing or automation.

# Database migration playbook

Use for schema, migration, persistence-model, or durable-history changes.

## Boundaries
- Read the persistence AREA contract and adjacent migration history.
- Prefer additive, reversible migrations.
- Never fabricate historical rows merely to make new history tables look complete.
- Preserve downgrade safety; if data loss would occur, refuse downgrade explicitly.

## Required verification
- migration upgrade and downgrade on the repository's normal local database;
- focused persistence tests and affected integration tests;
- PostgreSQL runtime validation when behavior depends on PostgreSQL semantics;
- offline generated SQL is evidence of SQL generation only, not runtime PASS;
- restart/readback for durable state when relevant.

## Evidence
Record revision id, parent revision, tables/columns changed, upgrade/downgrade result, runtime database used, tests, limitations, commit SHA, PR head, and exact-head CI.
