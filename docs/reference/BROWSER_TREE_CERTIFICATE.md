# Maintainer reference: private browser-tree closure certificates

> **Reference baseline:** [b73750e37c487a008dfdd2277440a2559d98ecf4](https://github.com/MulattoTech/MJobs/commit/b73750e37c487a008dfdd2277440a2559d98ecf4). All source/test line ranges refer to this revision, not floating main. Reviewed draft origin: 2f01b876762fcbdd70f11e69865a48257503af93; cited blobs are unchanged at this baseline. Static inspection is not runtime validation or requirement acceptance. Examples and maintainer cautions are guidance, not additional API behavior.

[Read the primary source](https://github.com/MulattoTech/MJobs/blob/b73750e37c487a008dfdd2277440a2559d98ecf4/services/applications/browser_tree_certificate.py).


## Pinned source and evidence boundary

- Source commit: `b73750e37c487a008dfdd2277440a2559d98ecf4`
- Source path: `services/applications/browser_tree_certificate.py`
- Full Git blob SHA: `588204696d1a4c78f1316314b62e78b79612e8dd`
- Static reference only. Tests and CI were not run; no acceptance or runtime claim is made.

This module stores private evidence that a particular enrolled browser-tree observer observed closure. The module docstring states the key boundary: recording a certificate never releases capacity; only an enrolled private observer can contribute a fresh native observation; callers cannot submit wire proof (`services/applications/browser_tree_certificate.py:1-7`). The application area describes the broader fixture-only browser flow and one-browser-at-a-time rule, but does not document this certificate’s enrollment/rollback/persistence contract; README and user-testing docs have no certificate-specific reference (`services/applications/AREA.md:1-52`; `README.md:1-117`; `docs/USER_TESTING.md:1-219`).

Unqualified line ranges refer to services/applications/browser_tree_certificate.py unless the paragraph explicitly names another source file.

## State and binding model

A certificate is written into `operations.recovery_metadata.manual_auth_owner.tree_closure` only for a `MANUAL_AUTH` owner in `RUNNING`, or in `BLOCKED` with `BROWSER_RELEASE_UNCONFIRMED` (`:28-45`). The supplied owner token is SHA-256 hashed and constant-time compared with the persisted owner digest after `validate_binding` reconstructs operation/application/attempt/profile scope (`:28-45`; binding helper: `services/applications/browser_tree_ipc.py:30-41`). The token itself is not stored by these functions.

Observer identities have a fixed shape: positive 32-bit PID, ASCII decimal creation identity up to 32 characters, and a 64-character lowercase hex host identity (`:19-26`). The enrolled observer must be on the same host identity as the identified API process (`:57-61`). SQLite writes acquire `BEGIN IMMEDIATE`; other databases rely on the transaction and row locking used by `_row` (`:47-55`).

## Public operations and actual lifecycle

The exported operations normally return booleans. Rejected types, scope changes and missing observations return false; database/audit failures and ordinary exceptions inside guarded blocks fail closed as `False`; sensitive storage/native diagnostics are intentionally suppressed (`:63-220`). These functions are private lifecycle machinery, not public HTTP endpoints. This is not a blanket no-throw contract: imports and some observer-property/type checks occur outside guarded blocks (lines 63-69,94-101,119-122,151-154,189-192).

### IPC observer path

1. `enroll_observer(store, operation_id, observer, *, owner_token)` accepts only an exact `BrowserTreeObserver` with valid identity. It requires the observer’s own binding to equal the validated operation scope, same-host identity, and no retained-query enrollment. First enrollment persists the observer identity and an enrollment audit event; an identical existing identity is idempotently accepted, while replacement is refused (`:63-91`).
2. `record_closure(store, operation_id, observer, *, owner_token)` requires the exact enrolled observer. It snapshots the observer identity, invokes `_record_closure` with `observer.observe`, and also verifies the identity did not change while observing (`:94-109`).

The IPC observer is created/used through `browser_tree_ipc`; bootstrap enrolls retained authority before acknowledging, then its persistence callback records evidence after closure observation (`services/applications/browser_tree_ipc.py:285-321`). A lost channel triggers a bounded wait for durable closure; channel loss or deadline alone is not treated as closure (`:312-329`).

### Retained native-query path

1. `enroll_retained_observer(store, operation_id, query, binding, *, owner_token)` accepts only an exact `BrowserTreeQuery`, captures the current observer process identity, checks the retention ID is a 64-character string and requires the observer PID to be outside the tree with no limit flags. It binds that identity and retention ID before relying on this native authority; repeating the same enrollment is accepted, but substituting a different query/IPC observer is not (`:145-181`).
2. `record_retained_closure(store, operation_id, query, binding, *, owner_token)` accepts only the exact retained query type. It again captures current process identity and retention key, requires the observer process to be outside the tree with no limit flags, obtains native accounting, and delegates to the same fenced writer (`:112-142`). It does not accept a serialized/wire observation instead of the native query.
3. `rollback_unlaunched_enrollment(...)` compensates bootstrap only when the same enrolled retained query remains outside the tree, has zero flags and exactly zero accounting, has no closure certificate, and matches owner/token/host. It removes only `tree_observer` and `tree_retention`, updates metadata and emits a rollback audit event; an admitted tree or any unknown state is not rollback-eligible (`:183-218`).

### Shared fenced persistence

`_record_closure` reads a baseline row/metadata, verifies exact scope and enrollment, then observes **outside** the write transaction. It accepts only the exact reply keys `binding`, `status`, `accounting`; the binding must match, both reply status and `_status(accounting)` must be `EXITED`, `terminated` must be zero, and the identity/query must remain unchanged (`:221-244`). It creates a version-1 certificate with binding, observer identity, accounting and observation time, then opens a transaction, locks/re-reads the operation and refuses to write if scope or recovery metadata changed since the baseline (`:245-258`). The certificate and `MANUAL_AUTH_TREE_CLOSURE_OBSERVED` audit event are committed together (`:245-258`).

If an equivalent certificate already exists, the writer returns true only when version, binding, observer and accounting match; `observed_at` is intentionally not part of that idempotency comparison (`:250-253`). The writer changes only recovery metadata and audit history. It does not transition operation state, cancellation, lease, application or attempt state (`:3-7,94-109,245-258`).

## Recovery is a separate action

`manual_auth_recovery.confirmed_tree_closure` validates the persisted proof against the enrolled observer, retention identity, binding and EXITED accounting (`services/applications/manual_auth_recovery.py:36-60`). Only the separate `recover_browser_capacity` operation checks the selected application/attempt scope and departed API process, requires a MOCK/AUTH_REQUIRED application and the latest AUTH_REQUIRED attempt, then transitions a held operation to `FAILED/BROWSER_OWNER_RECOVERED` (`:63-109`). A certificate alone is not recovery, sign-in success, permission to retry, or evidence that capacity has already been released.

The durable private metadata is intentionally excluded from public operation records. Tests assert the certificate is not serialized publicly while the operation remains RUNNING and capacity admission remains blocked (`tests/unit/test_browser_tree_certificate.py:61-82`; database failure cases: `tests/unit/test_fleet_t12_certificate_db_failures.py:65-151`).

## Dependencies and error behavior

Dependencies include SQLAlchemy `select`/`update`, the operation/audit models and shared `now`, `BrowserTreeObserver` plus binding/status validation from the IPC module, and lazy imports for `BrowserTreeQuery` and current process owner identity (`services/applications/browser_tree_certificate.py:9-16,112-120,145-151,183-190`). The module does not launch or terminate browser processes.

Public boolean `False` means no proof was accepted or storage could not confirm it; it is not proof that the tree is closed. In particular, broad exception handling avoids returning native/database diagnostics and preserves the fail-closed capacity boundary (`:63-91,94-110,112-143,145-181,183-220`). A successful `True` from recording means only that the private certificate was already equivalent or atomically stored and audited (`:245-258`); use the separate recovery workflow for capacity release.

## Safe lifecycle sketch

This is non-executable guidance using placeholders, not a standalone script. Only trusted internal manual-auth bootstrap code should handle real store/query/token objects:

```python
if not enroll_retained_observer(store, operation_id, query, binding,
                                owner_token=owner_token):
    # Do not treat the tree as observed or release capacity.
    keep_the_existing_recovery_gate()

# Later, after a fresh native observation:
recorded = record_retained_closure(store, operation_id, query, binding,
                                  owner_token=owner_token)
# False means no proof; True still does not itself release capacity.
```

Never manufacture a certificate from browser exit status, a PID check alone, IPC/wire data, a lost channel or elapsed time. Do not log/pass owner tokens or serialize private certificate metadata (`:1-7,112-142,221-258`; tests: `tests/unit/test_browser_tree_certificate.py:178-363`).

## Nearby regression evidence (not rerun)

- `tests/unit/test_browser_tree_certificate.py:61-363` exercises private certificate persistence/idempotency, held capacity, wrong token/replaced observer, live/terminated/malformed/scope-negative observations, concurrent owner changes, blocked versus terminal scope, retained-query authority and foreign-host refusal.
- `tests/unit/test_browser_tree_durable_bootstrap.py:28-184` covers enrollment-before-ack, wrong/stale owner, lost-ack unlaunched rollback, creator death, durable closure and public-record secrecy.
- `tests/unit/test_fleet_t12_certificate_db_failures.py:65-151` covers connection/audit/commit failures, rollback, retry, and continued capacity hold.

These tests were inspected as source, not executed during this documentation task.
