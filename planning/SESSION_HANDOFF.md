# MJobs — Codex-only integration handoff (October 10, 2026)

**One active checkout:** `C:\Tools\Dev\MJobs` · **GitHub:** `MulattoTech/MJobs` · **publication branch:** `main`. The GitHub branch is verified to be the sole remote head. Get the current exact commit from `git rev-parse main` and `git ls-remote origin refs/heads/main`; the mainline reconciliation commit is `6c7140c4`.

## Workflow and Git cleanup
- `AGENTS.md`, `CODEX_START.md`, and `docs/GITHUB_WORKFLOW.md` define one serial development/integration path using Codex CLI, Codex VS Code/Desktop, or explicitly authorized ChatGPT Web. Former parallel-lane prompts, configs and startup scripts were removed from the current tree. The root `planning/CLAIMS.json` is empty; previous #114 hold was explicitly released.
- All **447 GitHub issues** have exactly one `priority:*`, one `category:*`, and one `area:*`. 20 previously generic categories and 21 initially untriaged functional areas were manually refined; no generic `category:other` or `area:untriaged` remains. 20 priority labels have inference markers for future review. Use `tools/mjobs_issue_labels.py` to audit/add future missing labels.
- **GitHub branch heads:** one, `main`. 121 retired remote branch names removed using exact-SHA leases; 11 remote unmerged tips first archived as verified GitHub tags.
- **Local branch refs:** all 1,021 original heads archived under `refs/archive/mjobs/20261010/local/`. Of those, 997 obsolete refs were retired. The remaining 23 secondary heads are checked out in dirty or external worktrees; preserve them until in-progress files and running Codex sessions can be safely reconciled. 952 clean internal worktrees were safely detached without file changes; 25 unmodified worktrees were removed. Other historical directories remain physically present but are **not** active development paths.
- Old root ZIP/bare mirror, zero-length diagnostic file, and untracked experimental AI source/tests were moved unchanged to the external recovery folder, not committed as if feature-complete. `events.jsonl` is ignored as generated local diagnostics.

## Acceptance and validation
- **Original MJ-REQ:** 102 PASS / 109 PARTIAL / 155 NOT_RUN (366). **MJ-REF:** 19 PASS / 59 total. Product/release completeness has **not** been established.
- Clean tracked Python unit suite: **1,231 passed, 611 subtests passed**. Frontend Vitest: **765 passed**. TypeScript/Vite production build: **PASS** (existing oversized-chunk warning). Catalogue, milestone journal, agent skill and progress dashboard validators: **PASS**. AI commit-provenance validator available under `tools/verification/commit_provenance.py`.
- Hosted CI, production PostgreSQL/Redis end-to-end checks, live third-party job submission and production deployment: **NOT_RUN**. External submission remains **OFF**.
- Prior feature batch on #209/#169/#156/#171/#261 remains **PARTIAL** where full parent acceptance is unproven. Do not mark the project 100% on the basis of this clean stopping point.

## Recovery
- Local immutable recovery: `C:\Tools\Dev\MJobs-Reconciliation-Backup-20261010` (verified 2,183-ref Git bundle, original implementation journal, original handoff, exact branch SHA manifests, remote tag/branch report, archived experimental files).
- Redundant GitHub backup: [Historical Git bundle release](https://github.com/MulattoTech/MJobs/releases/tag/mjobs-history-backup-20261010) — explicitly **NOT a product release**.
- Journal reduced from 289 entries to the latest checkpoint; original saved and available via Git history. Derived `planning/REQUIREMENT_INDEX.json` is under 100 KB and rebuilds from authoritative acceptance sources.

**Next authorized development:** read `AGENTS.md`; check clean main; select the highest-impact unblocked P0/P1 issue; implement a testable end-to-end feature, update evidence, commit with `AI-Agent` / `AI-Model` / `Requirement` / `Validation` / `UTC-Date` trailers and safely publish. Do not reactivate old lanes, infer PASS from merge, or erase occupied secondary worktrees.
