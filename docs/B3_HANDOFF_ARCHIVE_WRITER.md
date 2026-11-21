# B3 immutable handoff archive writer — local-only preparation

**Status:** Additive archive writer implemented and tested in an isolated dependent
branch. The current planning/SESSION_HANDOFF.md remains the canonical source.
No actual historical handoff archive, summary cutover, deployment, acceptance
change, or publication of private checkpoint bytes is authorized or performed
by this phase.

This work depends on the separately reviewed byte-exact characterization in
PR #1070. Do not merge or run it against an unsupported version of
tools.verification.handoff_checkpoints.

## Purpose and safety boundary

The archive writer prepares the future B3 migration without changing the
shared continuity file. A default invocation is read-only. Writing is opt-in
and requires both an explicitly selected existing destination directory and a
full lowercase SHA-256 pin of the exact legacy input bytes. A mismatched
source digest rejects the request before any snapshot is created.

The selected destination must be outside this repository **and outside every
other Git checkout**. Archive bytes may contain private career, identity,
or operational history. Choose a privately controlled local directory with
appropriate filesystem protections, not a public repository, automatically
synced folder, or build artifact upload destination.

### Commands (illustrative; no real handoff export executed)

From the MJobs repository root, generate a content-free plan:

    python -m tools.verification.handoff_archive --source planning/SESSION_HANDOFF.md

After an operator independently reviews the source and destination, creates
a suitable private directory, and pins the exact digest from that plan, the
explicit archive operation is:

    python -m tools.verification.handoff_archive --source planning/SESSION_HANDOFF.md --destination PRIVATE_DIRECTORY_OUTSIDE_ALL_GIT_CHECKOUTS --expected-sha256 CURRENT_64_CHARACTER_SHA256 --write

An archive is written as:

    DESTINATION/sha256-FULL_DIGEST/manifest.json
    DESTINATION/sha256-FULL_DIGEST/pieces/ORDINAL-CONTENT_DIGEST_PREFIX.bin

Verify one independently stored snapshot without needing the source file:

    python -m tools.verification.handoff_archive --destination FULL_SNAPSHOT_DIRECTORY --verify

These commands emit only status, digest, byte count, and checkpoint count.
Neither manifest nor CLI report includes historical checkpoint text. The
content-addressed .bin files contain the exact original bytes and should
be treated as sensitive.

## Invariants and failure behavior

1. All snapshot sections are raw bytes from the pinned source. BOM, CRLF/LF,
   repeated or nested headings, fenced headings, and absent final newline
   are preserved exactly by the independently tested recovery validator.
2. Each checkpoint has an ordered offset, full SHA-256, and derived filename.
   Every checkpoint is reread independently from disk and the complete source
   is reconstructed and hash-checked before publication.
3. Writers acquire an exclusive create-only lock. An existing lock is not
   automatically broken or deleted. A stale lock requires an operator review.
4. New files are created exclusively inside a private temporary staging
   directory. The stage is verified before a same-filesystem directory rename.
   Partial write failures clean up only the newly owned stage and lock.
5. An existing valid archive is verified and reused unchanged. Corrupt or
   unexpected files, modified manifest metadata, missing sections, or symlinks
   fail closed; existing archives are never silently replaced or repaired.
6. The writer verifies that the source has not changed during the operation.
   It does not overwrite the canonical handoff, acceptance matrices, journal,
   shared task checkpoints, or any existing Git worktree.
7. The exclusive lock only coordinates compliant instances of this tool;
   external processes with write access to the destination remain outside
   its protection. File- and directory-level access controls are the
   operator's responsibility.

A sudden host power failure can leave a staging directory or writer lock.
Do not auto-delete those artifacts: inspect ownership and input identity before
recovery. The live handoff is not affected.

## Test and verification evidence

Synthetic-only test fixtures exercise write/readback, source deletion,
idempotence, missing/corrupt/reordered records, forged metadata, unexpected
files, lock preservation, injected write interruption, source mutation during
staging, rejection of Git-checkout destinations, and CLI output privacy.

Focused local test command:

    python -m pytest -q tests/unit/test_handoff_archive.py tests/unit/test_handoff_checkpoints.py

The focused suite passed **28 tests with one Windows symlink test skipped**.
Repository policy and whitespace checks passed. The skipped symlink test
must execute on a capable hosted runner before publication is considered
fully qualified. Full integration and hosted exact-head checks remain
NOT_RUN for this unpublished dependent source.

## Migration, semantic authority and rollback

This does NOT implement the generated current summary, decide which historic
checkpoint is authoritative, or change the current recovery path. Historical
claims may contradict one another; byte-perfect archiving alone cannot select
a correct current status or justify an acceptance promotion.

The next slice must independently review source ownership, generate a bounded
current summary with explicit checkpoint provenance, verify semantic recovery
for completed/blocked/unknown items, and demonstrate a protected rollback
before any consumer stops reading the legacy file.

Until that happens, rollback of this additive writer is simply to discard its
isolated feature branch or revert its future PR through normal protected Git
history. No production data migration or deployment is included.
