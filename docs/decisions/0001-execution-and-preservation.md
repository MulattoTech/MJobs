# Execution and preservation

The current user authorized complete autonomous execution of CODEX_START.md on
2026-09-29. AGENTS.md is the durable repository contract. Its instructions to
continue ordinary engineering take precedence over generic skill review pauses.
The existing area specifications and dependency queue are the design authority;
implementation plans refine them without replacing or deleting requirements.

The historical empty-remote baseline is outdated. GitHub main already contains
README.md at commit 4b8b6ed8e3b3e792d78e55df4742a7cd3b05244c. Preserve that
commit and file. Reconcile the control pack on codex/mj-pr-001-governance-safety
and a pull request, rather than overwrite or push directly to main.

The primary agent owns all checkout mutations. Supporting workers are read-only.
Use dependency-aware branches and at most four implementation PRs. Do not merge
into the shared main branch without explicit integration authorization. A PR
must contain implementation, executed evidence, and honest requirement status.

Real external job submission remains disabled. Local controlled mock submission
is the only submission permitted for testing. Docker engine availability is an
environment prerequisite for PostgreSQL/Redis Compose certification; local
tests cannot substitute for that evidence.
