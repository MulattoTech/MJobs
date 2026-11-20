# B3 cutover rehearsal — source-pinned, in-memory only

**Status:** Additive read-only safety proof. It does not replace the
canonical handoff, authenticate a reviewer, or authorize migration.

## Goal and preconditions

B3's persistent source is planning/SESSION_HANDOFF.md. Earlier steps provide
a lossless checkpoint inventory, an opt-in local archive writer, and a
reviewer-selected current summary preview. None of those establishes that
historical notes are semantically correct, complete, or currently authoritative.

Before switching any consumer to generated summary data, this rehearsal
requires three separately identified inputs:

- An original source file and its complete, reviewer-pinned SHA-256.
- A verified immutable checkpoint snapshot reconstructing exactly those bytes.
- A bounded, separately reviewed JSON selection with explicit checkpoint IDs,
  statuses, evidence, known unknowns, and an exact source hash.

The source and review files must be ordinary files, not symlinks. Archive
structure, checkpoint hashes and selection identities are checked by the
existing B3 verification tools before proceeding. Any mismatch fails closed.

## In-memory transition and rollback

The rehearsal first proves the archive reconstructs the original handoff
byte-for-byte, including byte order marks, line endings, headings and trailing
bytes. It then constructs only the reviewer-supplied summary preview.
After the simulated transition and rollback, it re-reads the source, review
selection and archive to reject any concurrent mutation of those inputs.
This is a postflight detection check, not a guarantee of exclusive locking
or authority to cut over any real reader.

A synthetic, in-memory consumer temporarily selects those summary bytes and
then restores the archived legacy bytes. The tool checks that the recovered
byte count and SHA-256 match the originally pinned file. It performs no write,
rename, deletion, source normalization, or live consumer configuration change.

The default output is metadata only. It reports source, preview, review, and
rollback hashes and states explicitly:

- Review authenticity: NOT_VERIFIED_BY_THIS_TOOL
- Semantic accuracy: NOT_VERIFIED_BY_THIS_TOOL
- Canonical cutover: NOT_AUTHORIZED_OR_PERFORMED
- Disk writes: 0

No PASS acceptance, production readiness, or user consent can be inferred.

## CLI

The following is an illustrative command; paths and hashes are placeholders
and must not be reused without actual local owner review:

    python -m tools.verification.handoff_cutover_rehearsal --source PINNED_SOURCE_FILE --snapshot PRIVATE_SNAPSHOT_DIRECTORY --review-file PRIVATE_REVIEW_JSON --expected-source-sha256 FULL_LOWERCASE_SHA256

To display the *reviewer-authored summary text* in the invoking terminal
rather than only the content-free validation report, add:

    --render-reviewed-preview

Neither form prints the full legacy handoff. Reviewer-authored preview text
may contain private operational information; use the render switch only in
a locally authorized context and do not paste output into public PR bodies.

## Synthetic verification contract

Tests exercise exact rollback and unchanged filesystem bytes, default
content-free CLI output, explicit rendering, incorrect or invalid source
pins, source mutation after archival, wrong review provenance, forged PASS
statuses, corrupted snapshots, missing review files, oversized input,
missing archives, and symlink refusal where the platform supports it.
Additional disposable synthetic-filesystem tests exercise atomic consumer
pointer replacement, archive-based restoration after a simulated crash,
and interruption before replacement. A separate Python process verifies the
restored file's SHA-256 after rollback, demonstrating process-boundary
readback on disposable synthetic data. The real handoff is never switched.

Run with the existing B3 tools:

    python -m pytest -q tests/unit/test_handoff_cutover_rehearsal.py tests/unit/test_handoff_current_summary.py tests/unit/test_handoff_archive.py tests/unit/test_handoff_checkpoints.py tests/unit/test_document_archive_history_window.py

The expanded local execution passed 78 primary tests and 16 subtests,
with 3 Windows symlink capability skips, 9 pre-existing SQLAlchemy
warnings, and no failures. Postflight negative controls reject concurrent
source, review-selection, and immutable-archive mutations during rehearsal.
The symlink skips require fresh Linux-hosted verification at the published
commit, and the exact-head repository policy gates remain mandatory.

## Remaining real migration blockers

This is an **in-memory demonstration**; it does not prove a process restart,
power-loss durability, concurrency-safe switching of a real consumer, or
semantic authority of any historical entry. Actual B3 cutover still requires:

1. An authorized integration owner to reconcile concurrent handoff writers,
   pin the exact main source, and approve the archival destination and
   confidentiality controls.
2. A complete review of current subject/status/evidence selections against
   real source and live GitHub acceptance facts, not merely a JSON claim.
3. A separate persistence/reader migration with atomic update, fresh current
   summary, recovery on process restart, and tested rollback after failure.
4. Fresh exact-head hosted checks, independent review, protected merge and
   an explicit cutover decision.

Until those steps pass, planning/SESSION_HANDOFF.md and the original
acceptance authorities remain unchanged and canonical. Do not silently
truncate or replace them, transfer historical private text to GitHub Actions
artifacts, or deploy a new release as part of this rehearsal.
