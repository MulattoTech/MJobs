# B3 private durable preview — additive, inactive by design

**Status:** A source-bound, write-once private artifact option for reviewed
current-summary previews. This stage never activates a reader, changes the
canonical handoff, imports private history into a public repository, promotes
acceptance, or deploys production software.

It depends on the existing verified B3 checkpoint archive, independently
reviewed current-summary selection, and synthetic rollback rehearsal. This
tool does not authenticate the selection's reviewer or prove their claims.

## Why this component exists

The existing read-only summary generator can render current-status text
in memory, but it cannot demonstrate that the resulting bytes survive
a fresh process or are protected against accidental replacement. This slice
creates an immutable *inactive candidate* in a private location outside
all Git worktrees. A future reader-migration PR can consume a separately
verified candidate only after the original owner reconciles all writers and
explicitly approves cutover and rollback.

The module does not create a symbolic link, active-current marker, routing
pointer, consumer configuration or automatic change to the existing handoff.
Its name intentionally describes a preview, not canonical authority.

## Preconditions and privacy

- A source handoff file and its exact full lowercase SHA-256.
- A byte-exact verified checkpoint archive for that same source.
- A bounded, reviewer-selected JSON input with checkpoint references,
  evidence and known unknowns. This input is task data, not proof of approval.
- For a write, a separately authorized and existing private destination
  directory **outside any Git checkout** and outside the source archive.
- The invoking user must protect the private directory with local ACLs.
  A destination containing public or automatically synced data is unsuitable.

Only synthetic fixtures were used in development. The actual historical
handoff and real reviewer selection have **not** been exported or written.

## Usage

Read-only plan (default):

    python -m tools.verification.handoff_private_preview --source SOURCE_FILE --snapshot VERIFIED_PRIVATE_ARCHIVE --review-file PRIVATE_REVIEW_SELECTION --expected-source-sha256 PINNED_64_CHAR_SHA256

Explicit write to an existing privately controlled directory:

    python -m tools.verification.handoff_private_preview --source SOURCE_FILE --snapshot VERIFIED_PRIVATE_ARCHIVE --review-file PRIVATE_REVIEW_SELECTION --expected-source-sha256 PINNED_64_CHAR_SHA256 --destination EXISTING_PRIVATE_DIRECTORY --write

After recording the 64-character preview digest from that report, verify a
complete existing candidate in a new process, without the source or review
file being present:

    python -m tools.verification.handoff_private_preview --verify --candidate COMPLETE_PRIVATE_CANDIDATE_DIRECTORY --expected-preview-sha256 FULL_PREVIEW_SHA256 --expected-source-sha256 PINNED_64_CHAR_SHA256 --expected-review-sha256 PINNED_64_CHAR_REVIEW_SHA256

Standalone verification requires all three trusted pins: original source
digest, reviewer-selection digest, and preview digest. A receipt that changes
the claimed reviewer selection while retaining the same preview bytes must
not pass against those independent pins.

The default output contains only result code, lengths, digests, selection
count, and the explicit NOT_AUTHORIZED_OR_PERFORMED cutover status. None
of these commands prints the reviewed summary. The saved preview.md is
private reviewer-authored text and must not be copied to public issue
comments, workflow logs, CI artifacts, or Git commits.

## Directory, integrity and failure behavior

An explicit write creates one candidate directory named
review-preview-sha256-FULL_PREVIEW_DIGEST with exactly two files:

    preview.md
    receipt.json

The receipt contains only the immutable source, reviewed-selection, and
rendered-preview hashes, byte length, count, version and non-active cutover
state. No historical source transcript is stored in the new candidate.
The original archive remains authoritative for recovering the legacy bytes.

The writer uses an exclusive create-only destination lock, restrictive
permissions before writing private bytes, staging-directory fsync, independent
staged readback, source/archive/review re-verification, and an atomic
same-volume directory rename. A pre-existing candidate is reverified and
never overwritten. A corrupt or unexpected candidate fails closed. An
unowned lock is never broken or deleted. An interruption before promotion
removes only this invocation's newly owned stage and lock.

A sudden power interruption can leave a staging directory, incomplete
directory metadata sync or an abandoned writer lock. Those require owner
inspection, not automatic cleanup or a fabricated durability guarantee.
The writer coordinates compliant instances; it cannot prevent a privileged
external process from changing the same filesystem. A verified candidate
does not establish the authenticity of its reviewer without independent
review of the pinned selection and its provenance.

## Verification and remaining work

Synthetic-only tests exercise the source/selection pin, preview digest and
UTF-8 content, fresh-process readback, repeat writes, missing or tampered
receipt/files, injected partial writes and source/review drift, owned/unowned
locks, private-path refusal and CLI opt-in boundaries. POSIX permissions and
symlink behavior require capable Linux-hosted verification when unavailable
on the local Windows host.

Run alongside the existing B3 chain:

    python -m pytest -q tests/unit/test_handoff_private_preview.py tests/unit/test_handoff_cutover_rehearsal.py tests/unit/test_handoff_current_summary.py tests/unit/test_handoff_archive.py tests/unit/test_handoff_checkpoints.py

The full B3 cutover remains **NOT DONE**. An integration owner must separately
verify the actual current semantic facts, single-writer ownership and privacy
controls, then build and review an atomic consumer transition with a complete
rollback plan. Protected exact-head CI and an explicit real-cutover decision
must precede any mutation of planning/SESSION_HANDOFF.md or consumers.
