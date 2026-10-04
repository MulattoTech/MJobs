# Developing MJobs in GitHub Codespaces

Use a Codespace when you want Python, Node, Docker, PostgreSQL, Redis, Playwright,
and Codex to consume GitHub's VM resources instead of the Windows gaming PC.

## Machine

As of 2026-10-04, the GitHub account exposes these MJobs machine types:

- `basicLinux32gb` — 2 cores / 8 GB RAM / 32 GB storage
- `standardLinux32gb` — 4 cores / 16 GB RAM / 32 GB storage

MJobs declares 4 cores / 16 GB / 32 GB as its minimum dev-container host
requirement. Use `standardLinux32gb`.

## Create the Codespace

Run this from a local PowerShell terminal with GitHub CLI authenticated:

```powershell
gh codespace create -R MulattoTech/MJobs -b main -m standardLinux32gb -d "MJobs Cloud" --idle-timeout 30m --retention-period 72h
```

The creation process runs `.devcontainer/setup.sh`. It:

1. creates the Linux `.venv`;
2. installs `requirements.lock`;
3. installs frontend packages;
4. installs Chromium and Playwright dependencies;
5. creates an ignored Codespace-local `.env` without replacing an existing one;
6. installs the current Codex CLI;
7. installs the lean Codex coding-skill set and PMM;
8. installs the vendored AFK-DEV skill;
9. validates project skills, catalogue, progress journal and the Git diff.

Open the Codespace in browser VS Code:

```powershell
gh codespace code -R MulattoTech/MJobs --web
```

If more than one Codespace exists, GitHub CLI will ask which one to open.

## First Codex login

Git/GitHub authentication is normally provisioned by Codespaces. Open a terminal
inside the Codespace and run:

```bash
codex
```

Complete the OpenAI login flow if Codex asks. This is the only authentication
step the repository cannot perform for you.

For normal MJobs development:

```bash
codex -m gpt-6-astra -c 'model_reasoning_effort="xhigh"'
```

Then instruct Codex:

```text
Read prompts/ASTRA_XHIGH.md and prompts/ASTRA_DELIVERY_CONTRACT.md, then begin authorized implementation.
```

Use Max for a difficult correctness blocker. Use Ultra only when the repository
has genuinely independent dependency-ready work; on the available 4-core
Codespace, keep write-heavy parallelism conservative.

## Run MJobs

Fixture/test stack:

```bash
bash .devcontainer/mjobs.sh fixture
```

Personal stack:

```bash
bash .devcontainer/mjobs.sh personal
```

Status:

```bash
bash .devcontainer/mjobs.sh status
```

Stop all Compose stacks:

```bash
bash .devcontainer/mjobs.sh down-all
```

Codespaces automatically forwards ports:

- 18700 — FIXTURE MJobs
- 18701 — PERSONAL MJobs
- 18702 — Development & Feedback

Keep forwarded ports private unless there is a deliberate reason to change
visibility.

## Verification

Fast environment/product checks:

```bash
bash .devcontainer/verify.sh quick
```

Full local regression:

```bash
bash .devcontainer/verify.sh full
```

The normal GitHub Verification workflow remains the merge authority for hosted
CI. The separate Codespaces workflow verifies that the dev container itself can
build and run its smoke checks.

## Development ownership

Treat the Codespace as the only writable MJobs checkout while it owns a delivery
session. Do not simultaneously let another Codex session edit the local Windows
checkout. GitHub `main` remains integration authority; use normal branches,
PRs, exact-head CI and review.

A stopped Codespace keeps its disk until its retention period expires. Push
valuable work before stopping or deleting it.

## Stop compute when finished

From the local PC:

```powershell
gh codespace list -R MulattoTech/MJobs
gh codespace stop -c <CODESPACE_NAME>
```

The configured idle timeout also stops an inactive Codespace automatically.
