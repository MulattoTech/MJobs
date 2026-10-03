# MJobs reconciled delivery implementation plan

> **For agentic workers:** Use superpowers:executing-plans for implementation. The primary owns integration; supporting audits are read-only.

**Goal:** Preserve live work, catalogue the 17 approved commercial gaps without duplicates, and continuously deliver eligible original requirements.

**Architecture:** Keep original requirement seeds and reference records separate. Add executable reference dependency validation; use GitHub heads, checks and merge receipts for publication truth, and capability evidence for product completion.

**Tech Stack:** Python, SQLAlchemy/Alembic, PostgreSQL, React, Playwright, GitHub Actions.

**Spec:** AGENTS.md; original area specs; user-approved MJ-REF-043..059 definitions recorded in REFERENCE_GAP_MATRIX.json.

## Global constraints

- Preserve all existing worktrees, untracked user templates and private ignored feedback.
- No real external submission, invented facts, access-control bypass or force pushes.
- Original MJ-REQ priorities precede optional reference enrichment.
- PASS requires executed acceptance evidence; merged incremental slices may remain PARTIAL.
- Required CI must match the final PR head, including any bookkeeping commit.

## Review focus

- An existing issue owns equivalent scope: map it, never manufacture a duplicate.
- A merged slice has unfinished requirements: preserve remaining scope and evidence status.
- A reference dependency is unknown or cyclic: reject the catalogue.
- A reference alias shares an issue: require an explicit equivalence record.
- Private files are untracked: preserve their hashes and exclude them from commits.

## Task 1: Reconcile and integrate verified existing work

- [x] Fetch origin; audit all 25 worktrees, branches, dirty files, issues and PRs. Local inventory is under the Git common directory in `afk-dev/reconcile-20261003`.
- [x] Review PR #453, run 256 Python tests and control-plane validation, verify exact-head CI and empty review threads, merge and fetch.
- [ ] Resolve any PR #452 integration conflicts without replacing either feature; revalidate and obtain new exact-head CI before merging.
- [ ] Record current publication truth while retaining historical evidence.

## Task 2: Catalogue and validate approved additions

**Files:** planning/REFERENCE_GAP_MATRIX.json, planning/REFERENCE_GAP_ISSUE_MAP.json, planning/REFERENCE_GAP_QUEUE.md, docs/reference/REFERENCE_PROJECT_GAP_ANALYSIS.md, docs/FEATURE_COMPLETENESS.md, tools/verification/catalogue.py, tests/unit/test_catalogue.py.

- [x] Audit all issue bodies once; show canonical mapping, overlap, prerequisites and priorities.
- [x] Add failing tests for 59 IDs, explicit equivalent issue mapping, missing feature scope and invalid/cyclic dependencies.
- [x] Create only missing issues, preserving stable markers for retries; save exact issue mapping.
- [x] Add feature records with acceptance criteria, owner, priority, overlap, prerequisites and test strategy; update validator and documentation.
- [x] Execute catalogue/progress tests, full regression, independent review, publication and exact-head CI; merge and reconcile.

## Task 3: Continue production vertical slices

- [ ] Select a dependency-eligible original requirement using current capability evidence and priority, not queue position alone.
- [ ] Read its live issue and area spec; record the small design and meaningful RED test.
- [ ] Implement, run focused/integration/browser tests as applicable, and record timestamped evidence and limitations.
- [ ] Update capability and queue state, commit/push normally, verify the exact latest head, review, merge and reconcile.
- [ ] Repeat while safe work remains. Preserve a durable next action at any genuine boundary.

## Execution checkpoint

PR453, catalogue PR471 and upload-integrity PR472 are merged after exact-head checks. Combined feedback integration has287 passing tests and awaits final PR452 CI. Source-quality MJ-REQ-083 is the next original foundation under investigation; full backlog and release readiness remain PARTIAL.
