# MJobs progress tracking — one writer, evidence-backed

The **authoritative acceptance sources** are `planning/CAPABILITY_MATRIX.json` (366 original MJ-REQ) and `planning/REFERENCE_GAP_MATRIX.json` (59 additive MJ-REF). They are counted separately. `planning/ISSUE_SEED.jsonl` supplies original priority and functional area; `planning/REQUIREMENT_INDEX.json` is the lightweight derived view. GitHub issues have `priority:P0..P6`, `category:*`, and `area:*` labels.

Only the currently authorized Codex integration session (or explicitly requested ChatGPT Web work) updates canonical acceptance. No claims, lane handoffs, concurrent planning writers, watcher tasks, or automated acceptance/progress commits. GitHub Actions may verify code, but cannot autonomously promote requirements to PASS.

**After a real accepted implementation batch:** record the exact revision, affected issue(s), executed local tests and failures/skips, remaining scope, and evidence; update only genuinely changed statuses. A limited slice remains PARTIAL even if its tests pass. Missing execution stays NOT_RUN. Then run:

```powershell
python tools/mjobs_reconcile_planning.py --apply
python -m tools.verification.catalogue
python -m tools.verification.progress validate
python -m tools.verification.progress_dashboard --write
python -m tools.verification.progress_dashboard --check
git diff --check
```

The generated `docs/PROGRESS_DASHBOARD.md`, `docs/PROGRESS_CARD.json`, and `docs/PROGRESS_CARD.svg` reflect recorded acceptance only. Do not claim entire project completion from individual merges. Only refresh after a meaningful accepted batch, or on an explicit user request.

**Stopping note:** `planning/SESSION_HANDOFF.md` should fit on one screen: local and remote SHA, pass/fail/not-run checks, release readiness, incomplete issues, blockers and next action. Historical evidence stays available in Git history and dated evidence records; do not paste entire transcripts into the handoff.
