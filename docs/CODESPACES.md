> **Current budget policy:** GitHub Actions, hosted tests, bot/cloud-agent reviews
> and extra AI reviewer sessions are disabled by the user. Older hosted-review/CI
> gates and expired deadlines below are historical where they conflict with
> AGENTS.md's active local-only policy. Use explicit local verification; missing
> hosted checks are NOT_RUN, never PASS. Do not restore billing or automation.

# Developing MJobs in GitHub Codespaces

Use a Codespace for a separate Linux development/test environment. Python,
Node, Docker, PostgreSQL, Redis, Playwright and Codex commands run on GitHub's
VM rather than the Windows PC. This does not move your Windows MJobs database
or private files into the cloud, and it is not a production deployment.

## Machine and creation

Choose **4 cores / 16 GB RAM** or larger. MJobs declares a minimum of 4 cores,
16 GB RAM and 32 GB storage. Select from the sizes GitHub actually offers your
account rather than relying on a historical machine-name snapshot. Linux parity
with CI and separation from personal data are the main benefits; a Codespace
is not inherently faster than a powerful local PC.

On repository **main**, choose **Code > Codespaces > New with options**, select
the machine size, and create. Check for an appropriate existing Codespace first.
Alternatively, from authenticated local PowerShell:

```powershell
gh codespace create -R MulattoTech/MJobs -b main --idle-timeout 30m
gh codespace code -R MulattoTech/MJobs --web
```

The CLI offers available choices and asks which Codespace to open when several
exist. No machine ID or shortened retention period is hard-coded. Review your
account's billing and retention settings before creating cloud resources.

Post-create setup installs locked Python dependencies, frontend packages,
Chromium/Playwright dependencies and Codex CLI. It creates an ignored local .env
only when absent and runs existing repository validators. It does **not** start
MJobs, launch an agent, sign you in, copy Windows files, or install global third-
party/PMM/AFK skills. It reuses `.agents/skills/mjobs-delivery` from this repo.

## First Codex login and task

In the Codespace terminal at the repository root:

```bash
codex --version
codex login --device-auth
codex login status
codex
```

Enable device-code login in your ChatGPT security settings when required. Open
the official link printed by Codex and enter its one-time code in your browser.
GitHub and Codex authentication are separate. ChatGPT sign-in uses subscription
access; API-key access has separate usage billing. Never commit or share auth
caches, tokens or login diagnostics, or copy your Windows .codex folder into Git.

Inside Codex, start with:

```text
Read prompts/CODESPACES_ITERATION.md. Reconcile current main and propose the
next smallest useful change. Do not implement until I approve that scope.
```

Then give the concrete issue or goal you want implemented. The same bounded
entrypoint can be passed directly into an interactive CLI session:

```bash
codex "$(cat prompts/CODESPACES_ITERATION.md)"
```

Use `/model` to select a model your account actually offers and `/permissions`
to review session permissions. No forced model, AFK mode, unrestricted approval
policy or autonomous backlog run is required. Installing Codex as a development
tool does not implement AI inside MJobs.

## Rerun setup or select a CLI version

```bash
bash .devcontainer/setup.sh
bash .devcontainer/bootstrap-codex.sh
# Optional example of an explicit version rather than the default latest:
MJOBS_CODEX_VERSION=0.160.0 bash .devcontainer/bootstrap-codex.sh
```

The version above is an example, not a required downgrade. Bootstrap attempts
the official npm package at most three times and prints the installed version.
It does not modify existing global skills, credentials or user configuration.
Older Codespaces may already contain previously installed PMM/AFK skills: the
new bootstrap does not delete user files. Use a fresh Codespace for new defaults
or review those older installations yourself.

## Start and stop MJobs explicitly

Prefer fictional fixtures for development. These are manual commands, not
automatic setup or prompt actions:

```bash
bash .devcontainer/mjobs.sh fixture
bash .devcontainer/mjobs.sh status
```

For an isolated synthetic Linux startup, set `COMPOSE_PROJECT_NAME=mjobs-fixture-`
followed by 12 lowercase hexadecimal characters and optionally `MJOBS_PORT`
(`1024`–`65535`) before running the fixture launcher. Personal overrides use
`mjobs-personal-` with the same suffix format. Inputs are validated before
configuration initialization. Without overrides, the project names and ports
remain `mjobs`/18700 and `mjobs-personal`/18701. The `status` and `down-*`
shortcuts always address those defaults; inspect or stop a disposable override
explicitly with `docker compose -p <project> --profile fixture ps` or `down`
(omit the fixture profile for a personal project). Keep its port set for Compose.

An optional separate personal stack is available, but real personal data should
not be introduced into cloud development by default:

```bash
bash .devcontainer/mjobs.sh personal
```

Default ports are 18700 (FIXTURE), 18701 (PERSONAL) and 18702 (feedback). Open the app
through the Codespace **Ports** panel/forwarded URL, not your PC's localhost.
Verify each port remains **Private**. Do not publish app or database ports.
The Codespace does not access or start the stopped Windows runtime.

Stop the MJobs Compose stacks using the existing helper:

```bash
bash .devcontainer/mjobs.sh down-all
```

## Verification and ownership

```bash
bash .devcontainer/test-bootstrap.sh  # Offline synthetic bootstrap contract
bash .devcontainer/verify.sh quick
bash .devcontainer/verify.sh full
```

`quick` checks environment, frontend/build and repository validators; `full`
also runs Python regressions. Hosted **Verification** remains product acceptance
authority. Devcontainer changes additionally require **Codespaces** smoke.
Inspect applicable checks and runtime artifacts on the exact final PR head.
Both workflows preserve PR merge-result testing and refuse a merge tree that
differs from the candidate; update the branch normally when main advances.
Never force-push or waive a failed gate.

Keep one writer per checkout and one integrator for overlapping work. Use a new
branch/coherent PR for each requested iteration. Before switching between
Windows and Codespaces, finish/checkpoint the current writer, commit and push
reviewed source, then fetch current GitHub state in the next environment.
Git push does not transfer ignored resumes, credentials, databases or outputs.

## Costs, stopping and preservation

Published personal quotas are 120 core-hours / 15 GB-month on GitHub Free and
180 core-hours / 20 GB-month on GitHub Pro. At four cores that is up to 30 or 45
active clock-hours when otherwise unused. Beyond included usage, four-core
compute is $0.36/hour and storage $0.07/GB-month. These rates were checked on
October 4, 2026; they are not a reading of your plan, remaining quota or bill.
ChatGPT/Codex usage is separate.

Closing a browser tab does not reliably stop compute; terminal output can count
as activity. Stop explicitly when finished:

```powershell
gh codespace list -R MulattoTech/MJobs
gh codespace stop -c <CODESPACE_NAME>
```

Stopping retains the disk until the applicable retention period expires, but
storage still consumes quota/can be billed. Automatic deletion can discard
unpushed changes. Push valuable reviewed source and privately back up needed
non-source data before deletion. Do not use a Codespace as the only copy of
private documents or production data.

Official references (recheck current account availability/pricing):
- [GitHub CLI creation](https://cli.github.com/manual/gh_codespace_create)
- [GitHub Codespaces billing](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)
- [Codespace lifecycle](https://docs.github.com/en/codespaces/about-codespaces/understanding-the-codespace-lifecycle)
- [Codex authentication](https://developers.openai.com/codex/auth/)
- [Codex CLI](https://developers.openai.com/codex/cli/)
