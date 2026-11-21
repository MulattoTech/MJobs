# B3 handoff checkpoint migration — recovery-first design

**Status:** Characterization-only proposal. No persistence migration, truncation,
archive publication, acceptance change, or shared continuity-file rewrite.

## Problem and source of truth

The current planning/SESSION_HANDOFF.md combines current and historical notes.
It contains mixed H1/H2 headings, repeated titles, nested subsections, older
status claims, and retained evidence links. Its order does not universally imply
that a section is the latest authoritative status. This file, Git history,
planning/agent_handoffs, and accepted evidence remain authoritative until a
separately reviewed migration explicitly changes that contract.

The B3 goal is to reduce onboarding context by generating a short current
summary from durable, immutable checkpoints without losing any historical
recovery information or changing existing acceptance identities.

## Phase 1: byte-exact characterization (implemented separately)

The read-only tools.verification.handoff_checkpoints module:

1. Opens the legacy handoff as raw bytes, without normalizing line endings.
2. Finds H1/H2 section boundaries outside fenced code blocks. H3/H4 headings
   remain embedded within their parent record. A preamble stays a record.
3. Computes ordered, content-addressed metadata with absolute offsets,
   individual SHA-256 hashes, and a whole-source SHA-256 hash.
4. Verifies that independently supplied section bytes reconstruct the original
   source exactly. Missing, reordered, modified, truncated, or unrecognized
   records fail closed.
5. Outputs only manifest metadata; no historical content, credentials, tokens,
   or copied personal context are emitted in the report.
6. Has no write, --apply, archive, branch, or deployment capability.

Run from the repository root:

    python -m tools.verification.handoff_checkpoints
    python -m tools.verification.handoff_checkpoints --json
    python -m pytest -q tests/unit/test_handoff_checkpoints.py

This baseline proves recoverability of a proposed byte segmentation. It does
**not** establish that a summarized document will preserve meaning, select a
correct authoritative checkpoint, or be safe to publish.

## Phase 2: future immutable archive — not implemented

After a single-writer ownership and overlap review:

1. Pin the exact main revision and original input SHA-256. Reconcile all dirty,
   concurrent, pending, and unmerged historical updates before any archive.
2. Materialize each exact section to immutable content-addressed files in a
   dedicated versioned directory, using an ordered manifest and atomic writes.
   Reject path escapes, mismatched digests, collisions, and writes that would
   mutate an existing checksum-named checkpoint.
3. Read back each saved file independently from disk; use the original recovery
   validator on those bytes and require exact equality with the untouched
   original handoff, including newlines, preamble, and trailing bytes.
4. Preserve original Git ancestry and all linked evidence, task IDs, acceptance
   statuses, and historical failure records. Do not deduplicate sections merely
   because their headings repeat.
5. Integrate a separately reviewed fixture and a documented rollback exercise.
   The initial archive should be additive and leave the original source
   byte-identical. Require strict exact-head hosted checks.

A successful export does not authorize altering the source document.

## Phase 3: generated current summary — not implemented

A compact current summary must have an explicitly designated, reviewed source
checkpoint for each subject. It must never guess from the first heading,
timestamp, source order, or the newest commit alone. Its generated entries
should expose subject, source checkpoint ID, exact evidence reference, status,
known unknowns, and the revision used for generation. The complete archive
must remain navigable and included in recovery documentation.

Before cutover, compare the generated summary with a manually reviewed current
state for representative active, completed, blocked, obsolete and historical
items. Verify that no historical FAIL, NOT_RUN, or UNKNOWN becomes PASS.

Only after source identity, byte restoration, semantic review, concurrency
safety, current-summary freshness tests, GitHub protected checks, and explicit
integration approval should a later PR change what readers use as the current
handoff. Preserve an independently reviewable original copy throughout.

## Rollback and stop conditions

- Before cutover: discard the additive manifest/archive branch; legacy source
  remains untouched.
- After cutover: restore the original content from the pinned Git blob through
  an ordinary protected PR and verify the original whole-source digest.
- Fail closed for unknown encodings, missing blobs, hash mismatches, altered
  checkpoint IDs, ambiguous current owners, uncommitted writers, or unresolved
  review requirements. Do not rewrite history or force-push.
- No product runtime, deployment, production data, access policy, acceptance
  matrix, or submission workflow changes are part of this work.

## Required test evidence before B3 completion

- Byte-exact restoration on mixed CRLF/LF, fenced headings, nested headings,
  duplicate headings, BOM, preamble, no-terminal-newline, and the real file.
- Negative controls for mutation, reordering, truncation, missing sections,
  invalid schema, and invalid offsets.
- Independently persisted checkpoint readback after a later archive writer.
- Atomic/partial-write failure and rollback tests for that later writer.
- Review of current-summary source selection and preserved recovery semantics.
- Focused unit, repository policy, catalogue, progress, dashboard, ownership,
  provenance, and fresh required hosted checks at the eventual integration SHA.

**This phase is an independently testable safety foundation, not a completed
B3 migration.**
