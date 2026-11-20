# B3 reviewed current-summary preview — no canonical cutover

**Status:** A read-only generator and synthetic tests are prepared in the
dependent B3 archive-writer worktree. The source archive writer remains opt-in.
No real historical archive has been created, no actual review selection has
been asserted, and no canonical handoff, acceptance record, or production
deployment has been modified.

## Why a reviewed selection is required

planning/SESSION_HANDOFF.md contains append-heavy mixed historical records.
A newer heading or timestamp does not prove that its content supersedes an
older checkpoint, that all acceptance criteria were completed, or that a
historical failure has been resolved. The generator MUST NOT infer a current
state from position, text similarity, commit order, or automatic extraction.

The input must instead be a separately reviewed JSON selection containing
explicit source checkpoint IDs and curator-supplied current summaries.
The immutable archive must first verify completely from disk; every selected
checkpoint ID must exist in that exact source manifest.

## Required input

A private UTF-8 JSON file, outside public repositories, with exactly:

    {
      "schema_version": 1,
      "source_sha256": "THE_FULL_64_CHARACTER_ARCHIVE_SOURCE_SHA256",
      "reviewed_by": "Reviewed operator identity",
      "reviewed_at_utc": "2026-11-20T18:00:00Z",
      "review_reference": "A specific durable review reference",
      "entries": [
        {
          "subject": "Selected subject",
          "checkpoint_id": "00000-CONTENT_SHA256_PREFIX",
          "status": "UNKNOWN",
          "summary": "One operator-reviewed sentence",
          "evidence": "Specific reviewed evidence",
          "unknowns": "Explicitly state what remains unverified"
        }
      ]
    }

Replace placeholders with actual reviewed values and exact checkpoint IDs
from the verified archive manifest. This example is NOT an approved current
summary. A generic document URL or an arbitrary JSON field is not proof of
independent approval; review authenticity must be checked by the integration
owner through the ordinary repository process.

Allowed workflow status labels are ACTIVE, BLOCKED, COMPLETE and UNKNOWN.
These labels are NOT canonical PASS acceptance, release readiness, or
permission to submit a live application. Every entry must supply evidence
and explicit unknowns. Duplicated subjects, unknown checkpoint IDs, missing
evidence, unsupported statuses, unrecognized fields, oversize text, control
characters and misleading bidirectional markers are rejected.

## Read-only commands

Both snapshot and reviewed input must be selected explicitly. By default,
the command validates them and prints only metadata, not the review prose:

    python -m tools.verification.handoff_current_summary --snapshot PRIVATE_FULL_SNAPSHOT_DIRECTORY --review-file PRIVATE_REVIEW_JSON

After a source/evidence review, explicitly render the reviewer-supplied
summary to the local terminal:

    python -m tools.verification.handoff_current_summary --snapshot PRIVATE_FULL_SNAPSHOT_DIRECTORY --review-file PRIVATE_REVIEW_JSON --render

No command writes a summary file, updates the live handoff, changes the
canonical matrices, selects a supposed newest checkpoint, creates a Git
commit, uploads any data, or deploys anything. Rendering includes only the
reviewed text and pinned checkpoint identifiers, not the full historical
source. Markdown metacharacters are escaped to avoid untrusted content
being rendered as links, headings, tables or HTML.

## Verification and remaining gates

Synthetic fixtures verify an independently reconstructed archive, source
digest binding, exact checkpoint selection, duplicate and invalid statuses,
required evidence, UTC review timestamp, schema limits, Markdown injection,
default content-free output, explicit render behavior, and no writes.

Focused combined execution:

    python -m pytest -q tests/unit/test_handoff_current_summary.py tests/unit/test_handoff_archive.py tests/unit/test_handoff_checkpoints.py

Observed local result: 48 primary tests passed, 2 Windows symlink
capability skips. Full hosted exact-head tests have NOT_RUN for this
dependent local feature branch. Linux-hosted checks must execute both
symlink cases before protected integration.

## Future semantic migration — not implemented

The final B3 cutover requires an actual operator-reviewed selection built
from current source/evidence, independently checked against the immutable
archive and real task state. It must preserve rollback to the original
source, establish a single writer, verify how readers resolve the generated
summary, and pass current-main protected CI. Only a separate reviewed
integration may switch consumers from the legacy handoff to generated
current-summary data.

The generated preview is not a replacement for the full historical archive.
Never promote a historic UNKNOWN, NOT_RUN, or FAIL merely because a
reviewed-summary JSON file contains a more optimistic sentence.
