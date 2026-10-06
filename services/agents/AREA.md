# Local agent tool boundary

Owns the typed tool catalog, proposal-only side-effect gate, and value-free
durable run traces for MJ-REF-013. It does not own model/provider calls,
browser sessions, database connections exposed to a model, or external
application submission.

## Public interfaces

- `ToolRegistry` registers server-owned JSON schemas and host-side handlers.
- `AgentRuntime` runs bounded tool-call batches with a host-bound profile/task
  context and records trace metadata in the existing append-only audit table.
- `AgentRuntime.review` revalidates the exact proposal, requires a human review
  decision, applies the registered deterministic policy, then executes a
  side-effect handler at most once.
- `build_default_registry(store)` registers task-scoped verified-profile reads
  and a local job-disposition proposal. It exposes no provider or browser tool.

## Requirements and dependencies

- Requirements: MJ-REF-013; preserves MJ-REQ-000, MJ-REQ-008, MJ-REQ-121,
  MJ-REQ-140; reuses the task allowlist from `services/career_profile`.
- Persistence: append-only `audit_events`; no new table or migration.
- Tests: `python -m unittest tests.integration.test_agent_tools -v`.

## Security constraints

Tool schemas contain data contracts only. Do not pass a Store, SQL connection,
filesystem, socket/network client, process, or browser handle to a model. Keep
tool arguments strict and bounded, traces free of raw prompts/arguments/results,
and side effects behind a matching proposal ID, explicit user review, and a
deterministic policy. Submission remains disabled and access-control/CAPTCHA
evasion is prohibited.
