# Job Lifecycle Implementation Plan

**Goal:** Implement MJ-REQ-085/086/087 in the existing canonical ingestion path.
**Architecture:** Append source-bound lifecycle observations and canonical material revisions;
store the current resolved status in canonical attributes. Reuse independent source authority.
**Tech Stack:** Python, SQLAlchemy Core, Alembic, PostgreSQL/SQLite, unittest.
**Spec:** `docs/spec/areas/job_lifecycle.md`; issues #86, #87, #88.

## Constraints and decisions

Work only in this worktree; no real submissions, main merge, or destructive downgrade.
The user's implementation instruction authorizes execution of this scoped design.
Use native execution with bounded read-only trace and independent final review.
Existing planning files provide durable state; no additional PMM system is needed.
`CLOSED` is distinct from REMOVED/EXPIRED/FILLED. No closure reason is inferred.
Native complete Greenhouse inventories can prove source removal. RSS/JSON feeds may
be rolling windows, so absence there is not removal evidence. Failed/partial reads
never establish absence. Unknown origins never acquire employer authority from payloads.
Observation timestamps are local collection times, not claimed posting/closure dates.
Material identity covers normalized whitespace in description/location, salary values,
remote type and restrictions; A -> B -> A retains three ordered revisions.

## Review focus

- Replayed and older observations cannot reopen a newer employer closure.
- Retargeted endpoints cannot lend authority to old evidence.
- Failed, partial, and bounded feeds cannot imply complete inventory.
- Missing salary/location/remote fields remain unknown in revisions.
- Concurrent refreshes serialize per canonical job; history is append-only.

## Tasks

- [x] Add failing lifecycle integration tests for states, first-party precedence,
  reactivation, repeated observations, immutable revisions and restart/API readback.
- [x] Implement `services/discovery/lifecycle.py`, two additive history tables,
  and hooks in candidate capture/value resolution and complete source collection.
- [x] Add native collector regressions for disappearance, rejection and failure.
- [x] Add Alembic migration with empty-only downgrade and migration regressions.
- [x] Run targeted suites, full practical regression, catalogue/progress validators,
  and independent review; fix substantive findings and rerun affected checks.
- [ ] Record exact evidence, update implementation state and queue, commit, publish
  draft PR if available, and report exact-head CI separately from local proof.
  Evidence/state/queue are updated in `docs/evidence/2026-10-03-job-lifecycle.json`;
  commit/publication remain BLOCKED_ENVIRONMENT below.

## Review and environment rulings

- Independent review found missing fixture lifecycle evidence and a potential
  complete-inventory PostgreSQL lock inversion. Both were fixed: fixture
  observations reference immutable raw snapshots, and the native collector locks
  its source row before all company/job changes. Independent re-review found no
  further concrete defects.
- Fixture observations have nullable candidate/quality references because that
  local-only pipeline has no production candidate records. Its evidence explicitly
  identifies the fictional source and immutable raw snapshot; it never asserts
  verified production employer identity.
- Git staging is BLOCKED_ENVIRONMENT: creating the shared worktree index lock is
  denied outside the writable root. No commit or publication can be claimed.
- Docker daemon access is denied. PostgreSQL offline DDL is separately validated;
  it does not establish PostgreSQL runtime or concurrent transaction acceptance.
