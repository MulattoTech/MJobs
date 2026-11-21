# B3 reviewed handoff preview for agent bootstrap — explicit only

**Status:** Optional, read-only compatibility stage. This is not a switch to a
generated current handoff, a new canonical acceptance source, or a migration
of the existing SESSION_HANDOFF.md file.

## The existing reader problem

`tools.agent_context.bootstrap` still reads
`planning/SESSION_HANDOFF.md` and returns the last 1,600 decoded
characters as `previous_handoff_tail`. The file currently contains
newer, prepended headings and older notes at the end. The tail is therefore
not a trustworthy way to select the current checkpoint. Changing the
default reader without a verified current-authority review would be unsafe.

This addition preserves the existing default output and makes a reviewed,
source-bound alternative available only on an explicit invocation.

## Inputs and read-only commands

The caller must supply **all three** of these inputs together:

1. A private, immutable B3 snapshot directory containing independently
   verified section bytes and their manifest.
2. A reviewer-authored JSON selection whose checkpoint IDs are bound to that
   source and include per-subject status, evidence and known unknowns.
3. The exact 64-character lowercase SHA-256 of the current tracked
   `planning/SESSION_HANDOFF.md` bytes.

From a verified checkout, inspect the preview **without printing the
reviewer-authored summary text**:

    python -m tools.agent_context bootstrap --task MJ-REQ-004 --handoff-snapshot PRIVATE_ARCHIVE_SNAPSHOT --handoff-review-file PRIVATE_REVIEW_JSON --handoff-source-sha256 EXACT_64_CHAR_SOURCE_SHA256

Explicitly include the reviewer-authored text in bootstrap JSON only when
local handling of that text is authorized:

    python -m tools.agent_context bootstrap --task MJ-REQ-004 --handoff-snapshot PRIVATE_ARCHIVE_SNAPSHOT --handoff-review-file PRIVATE_REVIEW_JSON --handoff-source-sha256 EXACT_64_CHAR_SOURCE_SHA256 --show-reviewed-handoff

The default command does **not** print the new reviewed summary text, but
retains all existing bootstrap output fields, including the legacy handoff
tail. Do not pipe bootstrap output into a public issue or PR without reviewing
it for potentially private history.

**Opt-in privacy suppression:** To omit the unreviewed legacy handoff tail
from the bootstrap result, add `--omit-legacy-handoff-tail`:

    python -m tools.agent_context bootstrap --task MJ-REQ-004 --handoff-snapshot PRIVATE_ARCHIVE_SNAPSHOT --handoff-review-file PRIVATE_REVIEW_JSON --handoff-source-sha256 EXACT_64_CHAR_SOURCE_SHA256 --omit-legacy-handoff-tail

This flag excludes `previous_handoff_tail` and reports
`legacy_handoff_tail_omitted=true`. It does not decode the raw historical
tail for output. The source may still be read **in memory** by the existing
reviewed-source integrity checks; the flag does not bypass source validation
or authenticate the reviewer. If `--show-reviewed-handoff` is also given,
reviewer-authored summary text is still explicitly printed. The suppression
flag can be used without a review selection, but **does not** confer current
handoff authority or perform a cutover.

Other bootstrap fields (task checkpoints, path previews and requirement
metadata) may contain sensitive information; this is **not** a complete JSON
redaction mechanism. Never publish unchecked bootstrap output.

The reviewed and privacy-suppression flags are restricted to `bootstrap`.
`status`, `resume`, `validate`, checkpoint creation and ownership
transfer preserve their existing behavior.

## Fail-closed and backward-compatible behavior

- Missing any of the three inputs fails; no partial option is silently
  ignored or accepted as a complete review.
- An untracked or locally modified canonical handoff file fails: preview
  source identity must be grounded in the assigned Git worktree. An
  unavailable Git dirty-state check also fails closed.
- The source file, archive and review selection must all match the explicit
  source checksum. Missing snapshots, corrupted sections, forged status,
  changed manifest, or stale review inputs are rejected by existing B3
  recovery validators.
- The caller receives the rehearsal validation metadata, including
  `review_authenticity=NOT_VERIFIED_BY_THIS_TOOL`,
  `semantic_accuracy=NOT_VERIFIED_BY_THIS_TOOL` and
  `canonical_cutover=NOT_AUTHORIZED_OR_PERFORMED`.
- Default bootstrap, existing per-task routing, requirement lookup, PR
  discovery and checkpoint behavior are unchanged.
- The code performs no archive creation, canonical write, PR mutation,
  provider submission, deployment or network upload.

A pinned review file is task data, not proof of independent approval.
The integration owner must verify the reviewer and semantic current-state
claims outside this tool.

## Test and remaining integration requirements

The synthetic tests create a fresh disposable Git repository, checkpoint
archive and reviewed selection outside the repository. They check default
output parity, success and failure of explicit source binding, opt-in text
rendering, missing arguments, stale source checksum, local dirty/untracked
canonical handoff, forged review identity, CLI scope and a separate-process
invocation.

The expanded focused preview suite passed **21 tests** on Windows,
including rejection of preview flags on other agent-context commands and
refusal to trust an unavailable Git cleanliness check. The combined
agent-context and B3 recovery suites passed **116 primary tests and 462
subtests**, with 4 skips and no failures. Repository policy,
catalogue, progress, dashboard, ownership, agent skills, syntax, whitespace
and commit provenance passed locally. These are local checks only;
fresh exact-head hosted CI remains mandatory before protected integration.

This is a transitional compatibility interface. B3 completion still
requires a separately reviewed current-state selection, reconciliation
of active handoff writers, durable consumer switching and rollback,
and explicit authorization for any real cutover.
