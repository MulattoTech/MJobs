> **Current budget policy:** GitHub Actions, hosted tests, bot/cloud-agent reviews
> and extra AI reviewer sessions are disabled by the user. Older hosted-review/CI
> gates and expired deadlines below are historical where they conflict with
> AGENTS.md's active local-only policy. Use explicit local verification; missing
> hosted checks are NOT_RUN, never PASS. Do not restore billing or automation.

Read CODEX_START.md and execute it completely. Treat AGENTS.md as the durable repository instruction contract. Re-verify local and GitHub state first; preserve any newer work. Do not perform real external job application submission without my explicit enablement.


# MJobs — Codex Start / Continue Directive

You are the primary implementation agent for **MJobs**.

Project:
- Local workspace: `C:\Tools\Dev\MJobs`
- GitHub: `MulattoTech/MJobs`
- Default branch: `main`

This control pack converts the original 8,246-line master directive into scoped working context.

## 1. Re-verify reality first
Inspect workspace contents/Git state, remotes/branch, GitHub files, and current issues/PRs.
Historical baseline as of 2026-09-29: local MJobs existed but was empty/not a Git repository; the private
GitHub repo existed on `main`, size 0, with no issues/PRs. If reality differs, preserve newer work.

## 2. Load only the control plane
Read `AGENTS.md`, `docs/AREA_ROUTING.md`, `docs/REPO_MAP.md`,
`planning/IMPLEMENTATION_STATE.json`, and `planning/PR_QUEUE.json`.
Do not read the full original master prompt unless resolving ambiguity or auditing traceability.

## 3. Establish source control if still uninitialized
If the package is in the still-empty local project directory and the remote is still empty:
- initialize Git on `main`;
- add `https://github.com/MulattoTech/MJobs.git` as `origin`;
- verify the remote;
- commit this control plane as one bootstrap commit;
- push `main`.

If the repo has gained content, reconcile this pack on a branch/PR instead. Never overwrite newer work.

## 4. Materialize the GitHub backlog idempotently
`planning/ISSUE_SEED.jsonl` is authoritative. It has every numbered source requirement plus independent
provider/integration child requirements.

Run:
`python bootstrap-tools/sync_backlog.py --repo MulattoTech/MJobs`

The sync uses stable hidden markers, resumes safely, avoids duplicates, and writes
`planning/GITHUB_ISSUE_MAP.json`. Commit that map afterward.

## 5. PR_QUEUE is a queue, not 100 empty PRs
The queue deliberately contains many small/coherent work units.
- default maximum 4 active implementation PRs;
- activate only dependency-unblocked entries;
- branch when implementation begins;
- open draft PR only after the first real commit;
- never create empty/dummy PRs;
- split broad queue entries while preserving requirement IDs;
- combine only tiny issues that genuinely form one atomic change.

## 6. Create implementation areas lazily
Use `docs/REPO_MAP.md`. When an area receives real work, create its directory and `AREA.md`, then implement.
Do not generate hundreds of empty modules/classes merely to make the tree look complete.

## 7. Prove a vertical product spine first
After control-plane bootstrap, advance the earliest unblocked queue item toward:
configuration/profile → mock source → normalized/canonical job → deterministic match →
mock company/ATS → application preparation → controlled mock submission → confirmation/tracking →
restart persistence.

That vertical spine proves architecture. Then expand source/company discovery, ATS coverage, matching,
documents, supported application automation, real read-only integrations, UX, diagnostics, and release verification.

## 8. Per-PR working set
For each PR load only:
- `AGENTS.md`;
- mapped issue(s);
- their single `docs/spec/areas/*.md`;
- relevant `AREA.md`;
- touched code/tests.

Then inspect → implement → targeted tests → fix → affected integration/E2E → update state/evidence →
commit/push → create/update draft PR → mark ready only with executed evidence.

## 9. Continue autonomously
Do not ask what to do next when the queue has an authorized unblocked item.
Continue until the evidence-backed Definition of Done, a genuine blocker, or user authorization is required
for a consequential action such as a real external job application.

Use `prompts/CONTINUE_NEXT_PR.md` in new sessions and
`prompts/RECOVER_INTERRUPTED_SESSION.md` after interruptions.

## 10. Never optimize for appearances
Do not inflate adapter counts with stubs, weaken tests to get green, mark unexecuted behavior PASS,
or build a polished shell around disconnected data. Completion means coherent, durable, truthful,
recoverable, verified end-to-end behavior.
