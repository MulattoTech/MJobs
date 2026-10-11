# MJobs — single-workspace Codex handoff (2026-10-10)

**Workspace:** `C:\Tools\Dev\MJobs` · **Remote:** `https://github.com/MulattoTech/MJobs` · **Branch:** `main`. The authoritative current SHA is `git rev-parse main`; verify `git ls-remote origin refs/heads/main` before publication or new edits.

## Completed reconciliation
- Current single-writer guidance: `AGENTS.md` and `CODEX_START.md`; no new parallel lanes, claims, or independent worktree creation. Root `planning/CLAIMS.json` has zero claims; old claim #114 explicitly released.
- All 447 existing GitHub issues have at least one priority, category, and area label. Inferred priorities were flagged for human triage. `python tools/mjobs_issue_labels.py` returns zero pending.
- The derived `planning/REQUIREMENT_INDEX.json` covers 366 original and 59 reference-gap items in under 100 KB; `python tools/mjobs_reconcile_planning.py --apply` rebuilds it.
- The 289-entry implementation journal was reduced to its latest milestone. The original 1.08 MB file and the original 312 KB handoff were copied to `C:\Tools\Dev\MJobs-Reconciliation-Backup-20261010`, along with a verified 2,183-ref Git bundle and worktree/ref manifests.
- Existing preserved historical source/evidence can be recovered from Git; no acceptance result was artificially promoted.

## Acceptance truth
- Original MJ-REQ: **102 PASS / 109 PARTIAL / 155 NOT_RUN** (366 total). MJ-REF: **19/59 PASS**. Full release readiness **UNKNOWN**, not 100% done.
- The previous local feature batch for #209/#169/#156/#171/#261 is only bounded acceptance; each parent remains PARTIAL. It has not been validated against live external services.
- Local frontend test run: **765/765 PASS**; TypeScript/Vite build PASS with an existing large-bundle advisory. Launcher/model tests and catalogue/journal validation passed. Check the current run's full Python unit result before making any broader test claim.
- Hosted GitHub Actions/reviews, full PostgreSQL/Redis Compose, and real ATS submission **NOT_RUN**. Real submission stays **OFF**.

## Recovery and next work
- Existing user-untracked files `MJobs-all-branches.zip`, `MJobs.git/`, `events.jsonl`, `services/ai/ai_failure_mode.py`, and `tests/unit/test_ai_failure_behavior.py` were not incorporated or deleted.
- Historical branch tips and dirty worktrees must not be treated as integrated. The backup Git bundle preserves refs, but uncommitted worktree files require separate preservation; never force-remove them.
- The next explicit Codex task should reverify main, select an unblocked P0/P1 **product** acceptance gap from GitHub, implement and locally test a coherent end-to-end slice. Keep this handoff short rather than reintroducing detailed per-agent planning.
