# MJobs

Local-first job discovery, factual document preparation, and application tracking.
The current user-testing slice includes a connected web application, a public
Greenhouse read-only source, and a controlled fictional application fixture.
Real external submission is disabled.

With Docker Desktop running Linux containers, start from PowerShell:

```powershell
cd C:\Tools\Dev\MJobs
.\Start-MJobs.ps1
```

The command preserves local data, builds the application, applies migrations,
checks the workers, and opens http://127.0.0.1:18700. See
[the user-testing guide](docs/USER_TESTING.md) for the connected journey,
diagnostics, verification commands, and current limitations.
Run the same command after Docker Desktop restarts to reconcile services without
deleting their named data volumes. The PostgreSQL, Redis, API, and worker
containers use `unless-stopped` restart policies; applying a newer Compose file
to an existing project requires another launcher run.

For a separate personal workspace without fictional discovery or mock application
actions, run `./Start-MJobs.ps1 -Personal` in PowerShell. It uses a separate
Compose project and data volumes at http://127.0.0.1:18701. Public read-only
source collection and factual manual preparation remain available. Real external
auto-submit stays off in both modes.

For an offline PostgreSQL and document backup, followed by a restore into a
separate empty Compose project, use the
[local backup runbook](docs/runbooks/LOCAL_BACKUP.md). Keep backup archives
outside this repository and protect them as personal data.

## GitHub Codespaces

MJobs includes a Codespaces/dev-container environment that mirrors the project
runtime with Python 3.12, Node 24, Docker Compose, Playwright and Codex tooling.
Use it when you want builds, tests, containers and AI development to run on the
Codespace VM rather than the local workstation. See
[docs/CODESPACES.md](docs/CODESPACES.md) for the one-command creation flow,
cloud startup commands, port forwarding, verification and shutdown.

Repository operating instructions live in [AGENTS.md](AGENTS.md). The complete
requirements remain in the GitHub backlog and scoped [area specifications](docs/spec/areas).
This milestone does not represent completion of the full specification.

## Reference-project gap tracking

MJobs also maintains an additive reference-project audit covering JobSpy, CareerWeaver, Resume Matcher, AutoApply, Personal JobPilot, ResuMorph, and AIHawk. See [docs/reference/REFERENCE_PROJECT_GAP_ANALYSIS.md](docs/reference/REFERENCE_PROJECT_GAP_ANALYSIS.md) and [docs/FEATURE_COMPLETENESS.md](docs/FEATURE_COMPLETENESS.md). The canonical additive backlog is `MJ-REF-001..042`; original `MJ-REQ` completion and reference-gap completion are reported separately.

## Live feedback development

Double-click **Start-Feedback.cmd** for the local **Development & Feedback** page:
19 guided tasks, automatic draft saving, restored sessions, and a saved-report
history. No manual template editing is needed, and it works without Docker.
The equivalent VS Code task is **MJobs: Development & Feedback**. Reports remain
private and local; **Ready for Codex** prepares the files for the next instruction
to process your feedback.

For browser-driven test/feedback development in VS Code, see [docs/LIVE_FEEDBACK_DEVELOPMENT.md](docs/LIVE_FEEDBACK_DEVELOPMENT.md). Checked-in VS Code tasks can start PERSONAL or FIXTURE mode and a privacy-conscious headed-browser recorder. Session data stays under ignored `outputs/live-feedback/`; use `prompts/VS_CODE_LIVE_FEEDBACK.md` with Codex to turn the latest observation into a test, patch, validation, and retest cycle.
