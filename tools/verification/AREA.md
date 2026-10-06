# Control-plane verification

Own catalogue completeness, dependency consistency and executed evidence tooling.
Requirements: MJ-REQ-007, MJ-REQ-008, MJ-REQ-281, MJ-REQ-290 through MJ-REQ-300.
Public interface: validate_catalogue(root: Path) -> list[str]; module CLI exits
nonzero on inconsistency. No GitHub mutations or product/personal data access.
Dependencies: Python standard library. Tests: python -m unittest discover -s tests.
New validators must test observable invalid inputs, not exact source text.

The fact-review browser verifier records source identity only when Git returns a
valid commit SHA and `git status --porcelain=v1 --untracked-files=all` confirms
the checkout is clean. Both commands have bounded deadlines and suppress Git
diagnostics; dirty or unavailable Git state fails the verifier and is recorded
as a stable error category without filenames or raw command output. Validate
this behavior with `python -m unittest tests.unit.test_verification_fact_review`
and `python -m tools.verification.fact_review --help` (the browser evidence run
itself requires the configured local Playwright/Chromium environment).

Implementation progress is recorded in `planning/IMPLEMENTATION_STATE.json` with
`python -m tools.verification.progress record --entry <evidence-json>`. Each milestone
contains the original progress-journal fields and a last safe Git/evidence checkpoint.
The CLI validates evidence paths and replaces the state file atomically; `validate`
checks the journal during CI. This control journal contains no personal application data.
After a terminal, Codex, browser or worker interruption, start with
`python -m tools.verification.progress resume`; it validates the journal and prints
the active issue, latest completed steps, remaining checks and last safe checkpoint as
JSON without changing state. Cross-process resume is covered locally; do not claim a
physical host or container restart from that synthetic boundary test.

Project-scoped Codex/agent workflows live under `.agents/skills/`. The `mjobs-delivery` skill is a small router with six on-demand playbooks for migrations, source adapters, application adapters, privacy/security, LLM providers/prompts, and release verification. Validate the contract with `python -m tools.verification.agent_skills`; the validator checks that routed playbooks, repository paths, and referenced Python command modules exist.

`gpu_independence` guards MJ-REQ-002 by checking required Python/JavaScript
dependencies, Docker/Compose runtime configuration, and frontend GPU APIs. It
does not inspect or modify host hardware. Its fixture tests cover valid CPU-only
inputs plus representative Python, JavaScript, Compose, Dockerfile, and frontend
GPU regressions. Run `python -m unittest tests.unit.test_gpu_independence` and
`python -m tools.verification.gpu_independence`.

`host_safety` guards MJ-REQ-001 by inspecting checked-in PowerShell, shell,
batch, and package-script setup entrypoints for broad host package upgrades,
driver/firmware/TDR edits, network resets, host shutdown/reboot, and broad
process termination. Project-scoped dependencies, containers, and local app
startup remain allowed. The verifier does not change host state or inspect
private feedback files. Run `python -m unittest tests.unit.test_host_safety`
and `python -m tools.verification.host_safety`.
