# Develop MJobs

Use one active integration owner. Open the repository root in VS Code or Codex CLI; temporary PR branches are not persistent multi-agent lanes.

## Reproducible tools

Python 3.12, Node.js 24 and Docker with Linux containers match CI. Keep tools project-scoped and leave existing personal application volumes untouched.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock -r .github/test-requirements.txt -r .github/docs-requirements.txt
npm ci --prefix apps/web
.\.venv\Scripts\python.exe -m playwright install chromium
```

On Linux, use `.venv/bin/python` and install Chromium system dependencies with `python -m playwright install --with-deps chromium`. Do not replace an existing environment unnecessarily.

## Fast feedback

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit
npm test --prefix apps/web
npm run build --prefix apps/web
.\.venv\Scripts\python.exe -m tools.ci.check_repository
.\.venv\Scripts\python.exe -m tools.verification.catalogue
.\.venv\Scripts\python.exe -m tools.verification.progress validate
git diff --check
```

Run affected integration/browser tests while implementing. The integration workflow provisions disposable PostgreSQL and Redis; it is not evidence that every opt-in external-provider test ran.

## PR workflow

Select an issue by priority and dependency; reproduce or define expected behavior, implement, run targeted tests, inspect the diff, then open a short-lived PR. Hosted checks verify a fresh checkout. Preserve source commits and exact check/run identities. Never merge around a failed required check.

Native auto-merge may wait for required checks without an artificial approving review. A test pass is neither human review nor a security certification. Do not silently approve major dependency upgrades.

Use the [automation guide](maintainers/AUTOMATION.md) for CI and the [release guide](maintainers/RELEASES.md) for publishing.

## Codex and AFK work

Read [AGENTS.md](../AGENTS.md). The owner may explicitly authorize the installed AFK-DEV skill. Its runner keeps one mutation-owner lock, fresh per-iteration context, anti-stagnation checks and independent completion commands. It does not remove account, approval, network or model limits.

Use the checked-in [sprint plan](../prompts/codex/next-plan.md), [goal](../prompts/codex/next-goal.md) and [main directive](../prompts/codex/next-main.md). The launcher [Start-CodexSprint.ps1](../Start-CodexSprint.ps1) defaults to a dry run; `-Run` is deliberate execution.

## Project synchronization

`python tools/maintenance/sync_project.py` audits the Project. Add `--apply` after a verified acceptance checkpoint to update it using your existing `gh` login and Projects permission. The command never changes issue states or acceptance evidence. No broad personal token is stored in public repository secrets.
