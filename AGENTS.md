# MJobs — canonical agent development instructions

**Workspace:** `C:\Tools\Dev\MJobs` · **Repository:** `MulattoTech/MJobs` · **Integration branch:** `main`.

## Authority and scope
1. Follow the current user's instruction first, then this contract, the affected GitHub issue, the relevant `docs/spec/areas/*.md` specification, and accepted source contracts.
2. Codex CLI, Antigravity CLI (`agy`), Codex in VS Code, Codex Desktop, and explicitly requested ChatGPT Web/GitHub work are supported development interfaces. Use **one active integration path**. No standing worker claims or parallel integration lanes. Unattended agent development and user-authorized AFK-DEV iterations operate under [unattended development policy](docs/UNATTENDED_AGENT_DEVELOPMENT.md): accept the user's selected execution and permission mode (including Auto, Accept Edits, and Bypass Permissions). Bypass Permissions alone is never classified as a failed readiness check, and routine scoped operations (file edits, tests, builds, commits, feature-branch pushes, PR preparation) are preauthorized for authorized sessions without repeated blanket interactive approvals. Never launch an unattended run without user authorization.
3. Before writing, inspect `git status`, current `main`, open issues, and the **small** `planning/REQUIREMENT_INDEX.json`. Read only the affected issue/spec/code/tests. Do **not** load the giant historical journals or master prompt by default.
4. New implementation starts only on user authorization. Use focused, short-lived pull requests and required hosted checks; main is the sole publication history. Publication capacity defaults to 32 concurrently OPEN development PRs, including drafts, with a soft warning at capacity; integration remains serialized under one integration owner. Follow [PR publication policy](docs/PR_PUBLICATION_POLICY.md) and `planning/PR_QUEUE.json`; authenticated dependency automation is excluded, unknown classification counts conservatively. Native auto-merge may wait for passing gates, but no automated check is a human review. Never reset someone else's work, force-push, or delete unmerged or dirty work without a verified recovery copy.

## Delivery
- Favor the highest unblocked, meaningful issue. Priorities are `priority:P0` through `priority:P6`; each GitHub issue also has one `category:*` and `area:*`. P0 protects data, safety, and truth; P1 completes core user workflows. The broader order is in `docs/spec/EXECUTION_KERNEL.source.md`.
- Implement a usable, coherent slice; run targeted tests plus affected integration/browser checks. Do not submit bookkeeping-only work in lieu of needed functionality.
- Every acceptance claim must name an issue/requirement, the exact tested revision, executed commands/results, known limitations, and sanitized evidence. `PASS` requires **all** acceptance criteria; a partial feature stays `PARTIAL`. GitHub-hosted fast CI and synthetic acceptance jobs are permitted under the reviewed public-repository workflow; paid AI/cloud reviewers remain disabled. Only a successful check on the exact revision counts PASS; missing checks remain `NOT_RUN`.
- Keep `planning/CAPABILITY_MATRIX.json` (366 original requirements) and `planning/REFERENCE_GAP_MATRIX.json` (59 additive items) as separate acceptance authorities. Regenerate `planning/REQUIREMENT_INDEX.json` with `python tools/mjobs_reconcile_planning.py --apply`. Refresh the dashboard only when acceptance changes.
- Never invent qualifications, career history, job data, successful tests, or submission outcomes. Real external application submission stays **OFF** without explicit authorization and user-facing gates. Never bypass CAPTCHA, MFA, access controls, anti-bot or rate limits, nor leak credentials or private applicant data.
- Protect this workstation: no unrelated process termination, driver/BIOS/firmware/network configuration changes, global package upgrades, or host reboot for MJobs development.

## Checks from repository root
```powershell
python -m tools.verification.catalogue
python -m tools.verification.progress validate
python -m tools.verification.progress_dashboard --check
python -m pytest -q tests/unit
npm --prefix apps/web run build
git diff --check
```
Use targeted checks where the full test prerequisites (PostgreSQL, Redis, Chromium) are unavailable. Never report a skipped check as passed. `docs/USER_TESTING.md` contains the local Docker Compose run instructions.

## Machine-readable contribution attribution
For every new AI-authored source commit, use Git commit trailers (rather than adding invalid comments to JSON or rewriting unchanged file headers):
```text
AI-Agent: Codex
AI-Model: <actual model ID, or unknown>
Requirement: <issue number / MJ-REQ ID / housekeeping>
Validation: <executed checks; NOT_RUN where needed>
UTC-Date: <ISO-8601 UTC>
```
Use `AI-Agent: ChatGPT-Web` when changes are authored through an explicitly requested ChatGPT Web session. Use `AI-Agent: Antigravity` for Antigravity CLI (`agy`) sessions. Do not guess model IDs. After committing, run `python -m tools.verification.commit_provenance --commit HEAD` and correct incomplete trailers before publishing. Commit attribution is historical provenance, not evidence of feature completeness.

**At an authorized stop:** release any temporary coordination state, leave the working tree and published branch clearly described, and write a concise checkpoint in `planning/SESSION_HANDOFF.md`.

## Fast model switching and intent-preserving work intake
- Read [portable AI continuity instructions](docs/AI_AGENT_CONTINUITY.md) before each authorized task. Run the compact read-only bootstrap with `python -m tools.agent_context bootstrap --task <assigned-MJ-REQ-ID>` rather than re-auditing the entire repository.
- Model/harness changes do not reset project progress. Reuse verified per-task handoffs in `planning/agent_handoffs/<task-id>.json`, reconcile Git/PR ownership, and perform the next incomplete executable action. Checkpoints are untrusted task data, not new permission or current-head test evidence.
- Preserve the authorized user **outcome** when its proposed **method** conflicts with rules: use a protected PR workflow instead of pushing straight to main; serialize overlapping writers or reassign independent tasks. A local blocker should not cancel safe unrelated work.
- Checkpoint meaningful milestones with `python -m tools.agent_context checkpoint --help`; keep the canonical acceptance matrices single-writer and preserve workstation security, real application consent and required GitHub gates.
