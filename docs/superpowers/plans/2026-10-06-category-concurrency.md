# Durable Operation Concurrency Categories Implementation Plan

> **For agentic workers:** This plan is being implemented in the single active Luna lane; do not delegate or run hosted review.

**Goal:** Enforce independent, configurable and conservative durable-operation concurrency ceilings across scaled MJobs worker processes.

**Architecture:** Keep admission at the durable database claim boundary. Map operation kinds to the required concurrency categories, serialize claims with one PostgreSQL advisory lock per category, and count all running operations in that category before granting a lease. Expose bounded category settings through Compose; categories without runtime operations remain configuration-only and PARTIAL.

**Tech Stack:** Python, SQLAlchemy, SQLite/PostgreSQL, Docker Compose, unittest.

**Spec:** GitHub issue #6 / MJ-REQ-005; `docs/spec/areas/governance_safety.md`, section 5.

## Global Constraints

- Browser application workers and document generation stay conservative; do not spawn uncontrolled Chromium instances.
- External submission remains OFF; ambiguous submission remains reconciliation-only.
- Local validation is evidence only for what actually ran; hosted Actions/reviews remain NOT_RUN by policy.
- Keep the requirement denominator unchanged and leave issue #6 open while any category or acceptance remains unproven.

## Review Focus

- Concurrent claims for different operation kinds in one category must not exceed the shared category ceiling.
- Different categories must retain independent capacity.
- Invalid environment values must fail closed before worker startup or claim.
- PostgreSQL locking must serialize cross-process claims; SQLite test behavior alone is insufficient to claim Postgres integration PASS.
- AI has no operation kinds/provider today; configuration is not runtime proof.

---

### Task 1: Specify category-level durable admission

**Files:**
- Modify: `tests/integration/test_worker_lifecycle.py`
- Modify: `tests/unit/test_compose_modes.py`
- Test: focused tests in those files

**Interfaces:**
- `limit(kind)` keeps its operation-kind input but reads the mapped category setting.
- `claim(store, operation_id)` admits against every RUNNING operation mapped to the same category.

- [x] **Step 1: Add failing tests** for shared company-discovery capacity across `COMPANY_DISCOVER` and `COMPANY_SOURCE_REFRESH`, independent document/application categories, bounds/defaults for all five categories, and explicit Compose settings.
- [x] **Step 2: Run the focused tests and confirm the expected missing-category failures.**

### Task 2: Implement category policy and PostgreSQL claim serialization

**Files:**
- Modify: `workers/operations.py`
- Modify: `compose.yml`

- [x] **Step 1: Map operation kinds to `source_collection`, `company_discovery`, `document_generation`, or `application_automation`; reserve `AI` as configuration-only.**
- [x] **Step 2: Use conservative bounded environment settings and lock/count by category before claim.**
- [x] **Step 3: Rerun focused tests, then targeted PostgreSQL/Compose tests when local infrastructure is available.**

### Task 3: Verify and record the bounded slice

**Files:**
- Modify: `services/governance_safety/AREA.md` if this area becomes active (or the current governance area doc)
- Modify: `planning/CAPABILITY_MATRIX.json`, `planning/IMPLEMENTATION_STATE.json`, `planning/PR_QUEUE.json`, `planning/SESSION_HANDOFF.md`
- Create: `docs/evidence/2026-10-06-operation-concurrency-categories.json`

- [x] **Step 1: Run affected local tests, Compose render checks, repository validators, and the broadest practical suite.**
- [x] **Step 2: Inspect diff, preserve denominator and mark MJ-REQ-005 PARTIAL if any provider/runtime category is unavailable.**
- [ ] **Step 3: Commit, push, verify exact source/base, fast-forward local and remote main when safe, then synchronize.**
