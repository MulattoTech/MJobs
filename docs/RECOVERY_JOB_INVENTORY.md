# Bounded recovery job inventory

The controlled Compose recovery verifier compares the current public job view for
one profile before and after restart. It hashes canonical job fields, application
routes, source-copy identities and stable source payloads. This is a visible
profile-context view: the API's existing defaults exclude hidden, not-interested
and blacklisted jobs. It does not inventory every row in the database.

## Completeness and budget

The verifier admits at most **200 visible jobs**. It requests `/jobs` with
`limit=201`: the extra row is a completeness sentinel, not another detail request.
If 201 or more rows are returned, collection raises the fixed error:

```text
Public job inventory exceeds the 200-job verification budget
```

An oversized response is refused before any per-job detail request. When this
occurs during initial recovery preflight, no recovery lifecycle command or write
is started and no successful recovery evidence is emitted. Exactly 200 jobs are
supported; collection makes one listing request and at most 200 detail requests.
The existing request timeouts and recovery target checks remain in force.

A default first page is insufficient evidence. Previously, a 201-job catalog
could produce a 200-job inventory and report PASS even after the omitted job's
source payload changed. Increasing the default page limit alone would only move
that false-success boundary. Catalogs beyond this budget require a future
bounded, complete enumeration contract; this verifier deliberately leaves them
unverified. Do not remove fixture jobs simply to make a recovery result pass.

The onboarding UI evidence command also uses this collector. A catalog above
the budget now fails baseline capture explicitly; it does not publish successful
onboarding evidence. Earlier UI actions may already have occurred. Similarly,
if a catalog grows beyond the budget during recovery, later collection fails
after earlier recovery actions, and the existing finalizer restores services.
The no-mutation guarantee above applies to the initial recovery collection.

## Historical baselines

New captures use inventory **version 3**, which identifies the bounded
201-row completeness probe. The validator admits only 1 through 200 jobs in this
version. The stored row fields, stable hashes and lifecycle timestamp exclusions
are unchanged.

Supplied version-1 and version-2 inventories are refused with the fixed error
`Legacy public job inventory requires a fresh complete baseline`. They are never
silently upgraded. A historical version-2 capture can contain only 200 of 201 jobs;
if its omitted job disappears, its count and digest can match a newly complete
200-job view despite the loss. A matching hash cannot establish that the earlier
capture was complete.

Keep historical evidence intact and obtain a fresh baseline with the current
collector in the intended controlled environment. A new complete 200-job baseline
remains supported. Both the recovery and backup-acceptance entrypoints reject a
supplied legacy inventory before Compose resolution, target preflight, HTTP
access, lifecycle actions or archive creation. Older baselines with no job
inventory retain the separate NOT_RUN result for that inventory check.

A matching visible job inventory is only one part of MJ-REQ-292; the complete
60-step end-to-end requirement remains PARTIAL. This change does not execute a
production restart, restore, external provider request or employer submission.

## Focused verification

```text
python -m pytest -q tests/integration/test_recovery_inventory_completeness.py tests/integration/test_compose_recovery_target.py tests/integration/test_api_queue.py tests/unit/test_compose_recovery.py tests/unit/test_backup_acceptance.py
```

The new boundary tests use an in-process public API and disposable SQLite
FIXTURE data. They cover actual default-page truncation, exactly 200-job admission,
source-payload changes, refusal before detail requests, encoded profile IDs,
legacy truncated baselines after deletion of the omitted job, and refusal before
recovery or backup mutation. Real Docker restart coverage remains a
separate gate and is not inferred from these tests.
