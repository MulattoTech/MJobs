# MJobs

> **Agent guidance and live tracker:** Every AI working on MJobs should read [AGENTS.md](AGENTS.md) and [batched progress-tracking policy](docs/PROGRESS_TRACKING.md). [The generated progress dashboard](docs/PROGRESS_DASHBOARD.md) is refreshed only at accepted integration checkpoints and closeout, not every turn. The next user-invoked core-feature sprint prompt is [here](prompts/LUNA_CORE_FEATURE_SPRINT.md).

> **Local-only development:** GitHub Actions and agent reviews are disabled by the user. Start with [the local Luna iteration prompt](prompts/LOCAL_ONLY_LUNA_ITERATION.md) and [current handoff](planning/SESSION_HANDOFF.md). Local verification, requirement completion and release acceptance are distinct.

Local-first job discovery, factual document preparation, and application tracking.
The current user-testing slice includes a connected web application, a public
Greenhouse read-only source, and a controlled fictional application fixture.
Real external submission is disabled.

For the current delivery checkpoint and next ChatGPT session, read the
[session handoff](planning/SESSION_HANDOFF.md) and
[copyable ChatGPT prompt](prompts/CHATGPT_WEB_HANDOFF.md).

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
Codex CLI is installed automatically in a new Codespace; sign in explicitly and
use [the bounded iteration entrypoint](prompts/CODESPACES_ITERATION.md). Setup
does not launch agents, start MJobs or install global AFK/PMM skills.

Repository operating instructions live in [AGENTS.md](AGENTS.md). The complete
requirements remain in the GitHub backlog and scoped [area specifications](docs/spec/areas).
This milestone does not represent completion of the full specification.

## Project progress

See the [colored progress dashboard](docs/PROGRESS_DASHBOARD.md) for the current evidence-backed original-requirement, reference-gap, PR-queue, active-work, and known-issue status. The dashboard deliberately separates merged GitHub state from local/uncommitted Codex work.

## Reference-project gap tracking

MJobs also maintains an additive reference-project audit covering JobSpy, CareerWeaver, Resume Matcher, AutoApply, Personal JobPilot, ResuMorph, and AIHawk. See [docs/reference/REFERENCE_PROJECT_GAP_ANALYSIS.md](docs/reference/REFERENCE_PROJECT_GAP_ANALYSIS.md) and [docs/FEATURE_COMPLETENESS.md](docs/FEATURE_COMPLETENESS.md). The canonical additive backlog is `MJ-REF-001..059`; original `MJ-REQ` completion and reference-gap completion are reported separately.

## Live feedback development

Double-click **Start-Feedback.cmd** for the local **Development & Feedback** page:
20 guided tasks, automatic draft saving, restored sessions, and a saved-report
history. No manual template editing is needed, and it works without Docker.
The equivalent VS Code task is **MJobs: Development & Feedback**. Reports remain
private and local; **Ready for Codex** prepares the files for the next instruction
to process your feedback.

For a product-level walkthrough, use [docs/GUIDED_PRODUCT_REVIEW.md](docs/GUIDED_PRODUCT_REVIEW.md) and the VS Code tasks **MJobs: Guided Product Review - Personal/Fixture**.

For browser-driven test/feedback development in VS Code, see [docs/LIVE_FEEDBACK_DEVELOPMENT.md](docs/LIVE_FEEDBACK_DEVELOPMENT.md). Checked-in VS Code tasks can start PERSONAL or FIXTURE mode and a privacy-conscious headed-browser recorder. Session data stays under ignored `outputs/live-feedback/`; use `prompts/VS_CODE_LIVE_FEEDBACK.md` with Codex to turn the latest observation into a test, patch, validation, and retest cycle.
