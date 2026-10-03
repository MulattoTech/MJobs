# MJ-REQ-083 authoritative source values

Base: main `96e48325793c29f74c96063abf36ea86c9180a2f`; PR473 merged,
no open PRs at reconciliation. All prior worktrees and user files preserved.

Replace last-write-wins canonical job imports with one coherent selected normalized
posting bundle. Preserve append-only candidates linked to the exact raw snapshot,
company source and collection observation. Never backfill invented source evidence.
Only compare postings already attached to the same canonical job; reject silent
posting reparenting and inconsistent source/company/tenant relationships.

Derive authority from verified domain evidence or a supported native ATS feed's
exact fixed origin/tenant relationship. Provider labels, source confidence and
user watch status are not verification. Rank authority before measured quality,
adjusted by its disclosed evidence coverage, then observation time and stable ID.
Unknown quality remains null. Select a whole bundle so salary/location and their
qualifiers cannot be mixed across sources. Match from the selected bundle.

Serialize collection writes on canonical jobs. Record quality observations and
candidates atomically with successful imports. Preserve every raw snapshot and
losing candidate. Keep existing application-referenced routes unchanged. Expose
the selected current posting URL separately; a disagreement with a preserved
route requires an explicit route choice before preparation, in both API and worker.
The UI explains source selection, conflicts, quality coverage and route review.

Validate observed RED for last-write-wins, reversed collection order, authoritative
source precedence, coherent bundles/matching, malformed attribution, snapshot
history, migration and rollback, prepared-route preservation/review, browser flow,
PostgreSQL concurrency/restart and affected full regression. Publish only after
local evidence; merge only after exact latest-head CI and independent review.

MJ-REQ-083 remains PARTIAL until application-link accuracy has genuine bounded
verification evidence. External application submission remains OFF.
