# B3 cutover readiness audit — local evidence, no activation

**Status:** This is an additive, read-only cutover *preflight*. It never changes
the canonical planning/SESSION_HANDOFF.md, switches active readers, writes a
private archive/preview, changes acceptance, or deploys an application.

B3's actual migration still requires a separately verified current-state
review, exclusive writer ownership, production-safe reader switching and
rollback, protected CI, and an explicit owner-authorized cutover decision.

## Why another gate is necessary

MJobs uses multiple Git worktrees and may also have independent Git clones.
A clean registered worktree is NOT evidence that a different checkout has no
uncommitted handoff content. Git worktree list cannot enumerate independent
clones. Omitting known independent checkouts could conceal a competing writer.
The audit deliberately fails closed on missing independent-checkout coverage.

Even a clean checkout with a valid private archive does not prove current
semantic accuracy, that a reviewer has authority, that no process will write
the handoff next, or that protected remote GitHub CI has passed. The output
explicitly reports all of those as unverified.

## Read-only local inventory

From a verified isolated repository checkout:

    python -m tools.verification.handoff_cutover_readiness --root MAIN_CHECKOUT

The default scan reports Git HEAD, local source SHA-256, handoff byte length,
registered worktree count, dirty/unverifiable registered worktree counts,
and blockers. It deliberately does NOT treat unenumerated independent clones
as clean. No historical handoff text or local worktree paths are printed.

Include each **known** independent Git clone when running the review:

    python -m tools.verification.handoff_cutover_readiness --root MAIN_CHECKOUT --independent-checkout OTHER_CHECKOUT_1 --independent-checkout OTHER_CHECKOUT_2

A separate checkout with an uncommitted handoff is reported in an aggregate
count and blocks the technical readiness result. Unknown/unreachable Git
roots and duplicate checkout paths also fail closed. The tool does not
discover every checkout that may exist elsewhere, inspect running processes,
or independently authenticate the caller's inventory statement.

If a reviewed synthetic or tightly controlled installation has no independent
clones, a caller may declare that explicitly:

    python -m tools.verification.handoff_cutover_readiness --root SYNTHETIC_MAIN_CHECKOUT --attest-no-independent-checkouts

This declaration is input data, not independently verified ownership or an
authorization to switch readers. Never use it to conceal a known checkout.

## Opt-in full technical evidence verification

The full check requires an exact source Git HEAD and source SHA-256, an
existing immutable archive, a private reviewed-selection JSON file with an
independent SHA-256 pin, and an existing inactive private-preview candidate
with an independent SHA-256 pin:

    python -m tools.verification.handoff_cutover_readiness --root MAIN_CHECKOUT --independent-checkout OTHER_CHECKOUT --expected-head SOURCE_GIT_SHA40 --expected-source-sha256 SOURCE_SHA256 --snapshot PRIVATE_ARCHIVE_SNAPSHOT --review-file PRIVATE_REVIEW_JSON --expected-review-sha256 REVIEW_SHA256 --candidate PRIVATE_INACTIVE_CANDIDATE --expected-preview-sha256 PREVIEW_SHA256

The full check validates:

1. Exact source commit identity and source byte digest; the repository must
   be on main, with the tracked canonical handoff intact.
2. Status of all registered worktrees and every independent checkout
   supplied by the caller. No uncommitted handoff may be silently ignored.
3. All private evidence lives outside Git worktrees, with no unreviewed
   archive/preview staging directories, active writer locks, symlinks or
   candidate/review files nested inside the immutable archive.
4. Independent reviewed-selection digest, source-pinned exact archive
   restoration, in-memory summary rehearsal and byte-exact rollback.
5. Private inactive candidate receipt and preview, independently bound to
   the supplied source, review and preview hashes.
6. A second local Git/worktree/independent-checkout inventory after private
   evidence validation. If the canonical source, another owner's handoff,
   registered worktree list, or source Git HEAD changes during the audit,
   technical readiness fails closed instead of reusing stale clean-state facts.

After the worktree postflight, it also re-checks the independent reviewed
selection digest, immutable archive reconstruction, private candidate digest
and writer-lock/staging paths. A review edit, corrupted archive piece, altered
candidate or new writer lock during the first verification fails closed.

These postflight checks are race-detection barriers, not process locks or a
durability guarantee. Other writers can still change state after the report.

All content inspection is in memory. Output contains status labels, hashes,
byte counts and aggregate worktree counts, not historical or reviewer prose.
The command never creates an archive, touches an active handoff reader,
runs an external model, or accesses the network.

## Interpreting the result and exit status

- Exit 2 / ready_for_review=false: one or more technical prerequisites are
  incomplete, unverified or blocked. This is the expected result for a
  basic inventory without a real private archive and reviewer selection.
- Exit 0 / ready_for_review=true: the **caller-supplied technical evidence**
  is internally consistent and no listed checkout has a detected handoff
  edit. This is NOT a cutover authorization or an independent fact review.
- can_cutover is ALWAYS false. canonical_cutover ALWAYS says
  NOT_AUTHORIZED_OR_PERFORMED.
- remote_protection is NOT_QUERIED. This tool never asserts live GitHub CI.
- review_authenticity, semantic_current_state and single_writer_authority
  remain unverified/not established regardless of local test results.

Actual cutover requires an integration owner to independently verify the
reviewer, source, active writers, release readiness, GitHub main revision,
durable reader transition, rollback and explicit permission. Do not infer
production authority from a JSON result or green synthetic test suite.

## Synthetic validation and privacy safeguards

Unit tests use only disposable Git repositories and synthetic private
archive/preview files. They include exact source/review/preview binding,
wrong pins, missing inputs, dirty registered and independent checkouts,
duplicate or unavailable independent clones, forged review metadata,
stale and unowned writer locks, unreviewed staging, misplaced private
evidence, CLI content privacy, and unconditional no-cutover behavior.

Focused command:

    python -m pytest -q tests/unit/test_handoff_cutover_readiness.py

Full B3 and agent-context suites, repository policy and protected Linux-hosted
exact-head CI must also pass before publication. The audit itself never
creates a real private artifact. No actual current-state review or live
handoff migration has been performed by implementing or testing this tool.
