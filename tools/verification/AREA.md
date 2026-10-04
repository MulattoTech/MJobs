# Control-plane verification

Own catalogue completeness, dependency consistency and executed evidence tooling.
Requirements: MJ-REQ-007, MJ-REQ-008, MJ-REQ-281, MJ-REQ-290 through MJ-REQ-300.
Public interface: validate_catalogue(root: Path) -> list[str]; module CLI exits
nonzero on inconsistency. No GitHub mutations or product/personal data access.
Dependencies: Python standard library. Tests: python -m unittest discover -s tests.
New validators must test observable invalid inputs, not exact source text.

Implementation progress is recorded in `planning/IMPLEMENTATION_STATE.json` with
`python -m tools.verification.progress record --entry <evidence-json>`. Each milestone
contains the original progress-journal fields and a last safe Git/evidence checkpoint.
The CLI validates evidence paths and replaces the state file atomically; `validate`
checks the journal during CI. This control journal contains no personal application data.

Project-scoped Codex/agent workflows live under `.agents/skills/`. The `mjobs-delivery` skill is a small router with six on-demand playbooks for migrations, source adapters, application adapters, privacy/security, LLM providers/prompts, and release verification. Validate the contract with `python -m tools.verification.agent_skills`; the validator checks that routed playbooks, repository paths, and referenced Python command modules exist.
