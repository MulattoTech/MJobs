# MJobs — Unattended Agent Development Policy

**Workspace:** `C:\Tools\Dev\MJobs` · **Repository:** `MulattoTech/MJobs` · **Integration branch:** `main`

## 1. Intent and Scope

This document establishes the authoritative policy for unattended, autonomous, and semi-autonomous AI-assisted software development in `MulattoTech/MJobs`. It applies across all supported development harnesses, including **Codex CLI**, **Antigravity CLI (`agy`)**, **Codex in VS Code**, **Codex Desktop**, and user-authorized multi-agent orchestration systems.

Unattended development is designed to maximize verified, high-quality production functionality delivered per elapsed hour without requiring human micro-approvals for routine development tasks, while maintaining strict repository safety invariants and auditability.

---

## 2. Core Governance Principles

### Rule 1: Multi-Harness Support
The repository supports Codex CLI, Antigravity CLI (`agy`), and other explicitly authorized agent harnesses as first-class development environments. Development scripts, verification tools, and documentation must not artificially restrict execution to a single vendor CLI.

### Rule 2: Acceptance of User-Selected Permission Modes
Agents and verification harnesses must respect and operate within the permission mode configured by the project owner. Supported modes include:
- **Auto / Interactive**: Prompts for tool approval according to harness policy.
- **Accept Edits**: Automatically approves file modifications while prompting on shell/command actions.
- **Bypass Permissions**: Preauthorizes tool and command execution for the duration of the authorized session.

### Rule 3: Permission Mode Is Not a Project Readiness Failure
The selection of `Bypass Permissions`, `Auto`, or `Accept Edits` is an operational runtime setting chosen by the operator. Readiness validators, linting scripts, and CI checks must **never** classify `Bypass Permissions` or unattended execution mode alone as a failed environment or project-readiness check.

### Rule 4: No Repeated Blanket Approvals for Authorized Sessions
When a user launches an unattended development session (e.g., via `/goal`, an authorized campaign prompt, or a launcher flag), the agent must not repeatedly prompt the user for routine, preauthorized actions within the authorized task scope.

### Rule 5: Preauthorized Routine Operations
Within an authorized unattended development session, the following operations are preauthorized and do not require interactive confirmation:
- Reading and inspecting repository files, git status, issues, and specifications.
- Editing source code, tests, documentation, and planning records within the active workspace/worktree.
- Executing local unit tests, integration tests, synthetic fixtures, and frontend/backend build commands.
- Creating local git commits with standard machine-readable attribution trailers.
- Pushing isolated feature branches (non-force) to remote repositories.
- Preparing, publishing, and updating GitHub Pull Requests against `main` within publication policy limits.

### Rule 6: Separation of Runtime Configuration from Repository Governance
Agent runtime execution configuration (such as model tier, reasoning effort, thread concurrency, context window management, and harness permission flags) is strictly decoupled from repository governance. Repository governance defines *what* may be integrated and *how* truth is validated; runtime harnesses define *how* agents interact with the local operating system.

### Rule 7: Non-Destructive Workflow, Ownership, and Auditability
- **Non-Destructive Git**: Never force-push (`--force`), rebase shared branches, or discard uncommitted or unmerged work without a verified recovery copy.
- **Worktree Ownership**: Concurrent agents must operate in isolated, disjoint worktrees or branches with distinct writable files. Competing writes to identical files are prohibited.
- **Durable Checkpoints**: Agents must record handoff state in `planning/SESSION_HANDOFF.md` before concluding an execution window.

### Rule 8: Compatibility with Codex Tooling and AI Attribution
All commits authored by AI agents during unattended development must maintain full compatibility with repository verification tools, specifically `python -m tools.verification.commit_provenance`:
- Commit messages must include valid git commit trailers (`AI-Agent`, `AI-Model`, `Requirement`, `Validation`, `UTC-Date`).
- Supported `AI-Agent` values include `Codex`, `Antigravity`, and `ChatGPT-Web`.
- Valid UTC ISO-8601 timestamps are required on all source commits.

---

## 3. Inviolable Safety Boundaries

Bypass Permissions preauthorizes routine tool actions; it does **not** grant unlimited authority or waive repository safety boundaries. The following boundaries remain strictly active and non-negotiable at all times:

1. **GitHub Protected Branches**: The `main` branch remains protected. All code integrates exclusively through reviewed, passing GitHub Pull Requests. Direct pushes to `main` are disabled and prohibited.
2. **Required CI Checks**: Integration requires exact-head passing CI on GitHub. No agent may disable, bypass, or forge CI gates.
3. **External Job Submission Remains OFF**: Real external application submissions to real employers, job boards, or ATS endpoints remain strictly disabled (`OFF`) without explicit, interactive, real-time user-facing consent.
4. **No CAPTCHA, Anti-Bot, or Access-Control Bypass**: Agents must never attempt to bypass CAPTCHA, MFA, Cloudflare, anti-bot mechanisms, rate limits, or authentication boundaries. Encounters with access barriers must result in `USER_INTERVENTION_REQUIRED`.
5. **No Secret Exfiltration**: Never commit, log, or transmit `.env` secrets, API keys, credentials, or private applicant personal information.
6. **Host Workstation Protection**: Do not execute destructive filesystem commands outside the workspace, terminate unrelated OS processes, modify system network/DNS/firewall settings, update global drivers/firmware, or reboot the physical host.
7. **No Fabrication of Evidence**: Acceptance claims (`PASS`) require executed, verifiable test evidence covering all mandatory acceptance criteria. A partial feature remains `PARTIAL`. Missing checks remain `NOT_RUN`.

---

## 4. Verification and Compliance

Compliance with this policy is verified automatically by repository tools:
```powershell
python -m tools.verification.catalogue
python -m tools.verification.progress validate
python -m tools.verification.commit_provenance --commit HEAD
python -m pytest -q tests/unit/test_unattended_agent_governance.py
```
