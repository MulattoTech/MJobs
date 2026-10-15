# MJobs — canonical Codex development instructions

**Workspace:** `C:\Tools\Dev\MJobs` · **Repository:** `MulattoTech/MJobs` · **Integration branch:** `main`.

## Authority and scope
1. Follow the current user's instruction first, then this contract, the affected GitHub issue, the relevant `docs/spec/areas/*.md` specification, and accepted source contracts.
2. Codex CLI, Codex in VS Code, Codex Desktop, and explicitly requested ChatGPT Web/GitHub work are the supported development interfaces. Use **one active integration path**. No standing worker claims or parallel integration lanes. User-authorized AFK-DEV iterations may continue with one mutation owner, independent verification and normal runtime/approval limits; never launch an AFK loop without authorization.
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
Use `AI-Agent: ChatGPT-Web` when changes are authored through an explicitly requested ChatGPT Web session. Do not guess model IDs. After committing, run `python -m tools.verification.commit_provenance --commit HEAD` and correct incomplete trailers before publishing. Commit attribution is historical provenance, not evidence of feature completeness.

**At an authorized stop:** release any temporary coordination state, leave the working tree and published branch clearly described, and write a concise checkpoint in `planning/SESSION_HANDOFF.md`.
