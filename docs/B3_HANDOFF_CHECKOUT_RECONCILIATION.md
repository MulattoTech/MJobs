# B3 checkout reconciliation inventory — read-only

**Status:** Synthetic-tested, opt-in preparation for an independent writer
ownership review. It does not select a current handoff, approve historical
claims, change a Git checkout, store content, replace readers or deploy.

## Purpose

Before replacing the legacy `planning/SESSION_HANDOFF.md`, an integration
owner must reconcile multiple versions from Git-registered worktrees and
independent clones. The earlier B3 readiness audit reports aggregate
differences; this utility adds **per-checkout metadata only** so a reviewer
can distinguish older committed history, uncommitted changes, hidden edits,
and operating-system checkout byte conversions.

The inventory is NOT proof that all active writers have been enumerated,
that a checkout is inactive, that a historical version should be discarded,
or that a given handoff's status is semantically accurate.

## Read-only invocation

The command requires no network, writes no reports or history files and
returns JSON on standard output:

    python -m tools.verification.handoff_checkout_reconciliation --root MAIN_GIT_CHECKOUT --independent-checkout ANOTHER_KNOWN_GIT_CLONE

Repeat `--independent-checkout` for each known clone. Omitting all
independent-checkout inputs leaves `INDEPENDENT_CHECKOUTS_NOT_ENUMERATED` as a
blocker. For synthetic-only configurations with none, use
`--declare-no-independent-checkouts`; that is caller-supplied information,
not independently verified exclusive ownership.

No checkout paths, branch names, reviewer names, original handoff paragraphs,
or raw source snippets are included in the resulting JSON. Rows use
ordinal IDs such as `registered-03` and `independent-01`. A local
operator can match ordinals to their own `git worktree list --porcelain`
output; that local path mapping is not written to source control. The tool
does return **hashes and commit IDs**, which are sensitive metadata in some
contexts. Avoid posting real inventory results to GitHub or public logs.

## Exact classification

Each enumerated checkout is checked against its own `HEAD` Git blob
and the current main handoff's exact raw bytes:

| Classification | Meaning |
| --- | --- |
| IDENTICAL_MAIN_BYTES | On-disk bytes match main; handoff status and Git blob are internally consistent |
| HISTORICAL_COMMITTED_VARIANT | Working bytes match that checkout's committed blob but differ from main |
| WORKTREE_BYTE_ENCODING_VARIANT | Committed Git blob matches main, but exact working bytes differ, e.g. LF/CRLF conversion |
| DIFFERENT_COMMITTED_BLOB_SAME_BYTES | Different tracked Git blob, despite identical current raw bytes |
| UNCOMMITTED_HANDOFF_EDIT | Working Git blob differs from checkout's committed blob, with visible Git status |
| GIT_STATUS_MASKED_HANDOFF_EDIT | Working Git blob differs, but Git status reports clean (e.g. assume-unchanged) |
| HANDOFF_STATUS_CHANGED | Git reports a handoff-related change despite matching raw bytes and blob |
| UNVERIFIABLE | Checkout missing, symbolic, untracked or unverifiable |

## Local Git head ancestry

Each checkout also has a `head_relationship` classification, and the
report aggregates `head_relationship_counts`:

| Relationship | Meaning |
| --- | --- |
| EXACT_MAIN | Checkout HEAD is the pinned current-main commit |
| ANCESTOR_OF_MAIN | The checkout is at an older commit already reachable from main |
| DESCENDANT_OF_MAIN | Checkout contains commits not yet in current main |
| DIVERGED_FROM_MAIN | Neither branch HEAD is an ancestor of the other |
| UNVERIFIABLE | The required objects or local Git ancestry cannot be verified |

This is a **local, read-only Git object-graph check**, not evidence about who
owns a checkout or whether its historical handoff is semantically relevant.
Missing objects in independent clones remain UNVERIFIABLE; the tool never
fetches, rewrites, merges, or assumes another checkout is safe to discard.

The scan separately detects a dirty or changed canonical main handoff,
unverifiable independent checkouts, and declared coverage gaps. It rescans
the checkout inventory and content fingerprints before returning. If a
concurrent mutation occurs, the result fails closed with
`CHECKOUT_STATE_CHANGED_DURING_RECONCILIATION`.

Checking the normalized working Git blob relies on Git's usual clean-filter
logic, while the SHA-256 fingerprint uses the **actual raw file bytes**.
This preserves byte-exact rollback concerns without incorrectly labeling
line-ending conversion as an uncommitted semantic revision.

## Meaning of a result

No inventory can authorize a real cutover. Even a fully matching inventory
returns `can_cutover=false`, `exclusive_writer_authority=NOT_ESTABLISHED`,
and `canonical_cutover=NOT_AUTHORIZED_OR_PERFORMED`. Exit 0 means only
that the caller-declared local checkout inventory had no observed blocker;
exit 2 means reconciliation or inspection remains incomplete. Neither
exit status proves production readiness.

Never discard or reset another agent's handoff to make the counts green.
The integration owner must separately identify writers, decide which
historical versions remain relevant, preserve and reconcile private
content, authenticate the reviewed current selection, and approve an
independently rollback-tested reader migration.

## Synthetic verification

Tests use only disposable Git repositories and fictional handoffs:

    python -m pytest -q tests/unit/test_handoff_checkout_reconciliation.py

Negative controls include stale committed branches, independent clones,
Git-hidden modifications, missing/untracked handoff sources, duplicate
checkout coverage, changed source bytes during inspection, and metadata
privacy. The normal B3, agent-context, repository-policy and Linux-hosted
exact-head CI gates remain mandatory before protected integration.

This is an additive, isolated development slice. The actual
`planning/SESSION_HANDOFF.md` and all other worktrees remain untouched.
