# Maintainer reference: durable discovery search history

> **Reference baseline:** [b73750e37c487a008dfdd2277440a2559d98ecf4](https://github.com/MulattoTech/MJobs/commit/b73750e37c487a008dfdd2277440a2559d98ecf4). All source/test line ranges refer to this revision, not floating main. Reviewed draft origin: 2f01b876762fcbdd70f11e69865a48257503af93; cited blobs are unchanged at this baseline. Static inspection is not runtime validation or requirement acceptance. Examples and maintainer cautions are guidance, not additional API behavior.

[Read the primary source](https://github.com/MulattoTech/MJobs/blob/b73750e37c487a008dfdd2277440a2559d98ecf4/services/discovery/search_history.py).


## Provenance and verification boundary

- **Source commit:** `b73750e37c487a008dfdd2277440a2559d98ecf4`
- **Source path:** `services/discovery/search_history.py`
- **Full source blob SHA:** `45fedefcaf80331322554e522e34ac21aaba7bc1`
- **Method:** static inspection of that immutable source blob and exact-commit API, event builder, tests, and historical evidence listed below.
- **Runtime status:** **NOT_RUN**. This draft does not claim test execution, CI status, PostgreSQL behavior, live-source access, or acceptance.

## Public API and dependencies

The module's declared role is durable, profile-scoped history for terminal discovery search runs (source line 1). Runtime dependencies are Python standard library (`base64`, `binascii`, datetime, JSON, math, regex), SQLAlchemy query operators, SQLite/PostgreSQL dialect inserts, persistence models, and deterministic `identity` (lines 4-18).

Only three terminal audit actions are listed: `SEARCH_RUN_COMPLETED`, `SEARCH_RUN_FAILED`, and `SEARCH_RUN_CANCELLED` (21). Records live in the existing audit table rather than a dedicated history table (144-166, 219-234).

## Canonical record contract

`_normalize_run(value)` at source lines 99-131 accepts a dictionary with exactly these keys and no others (22-25, 99-101):

```text
run_id, profile_id, source, query, filters, started_at, finished_at,
status, results, new_jobs, duplicates, errors
```

### Text fields

`_text` requires strings, strips leading/trailing whitespace, enforces a character maximum, and rejects code points below 32 (33-40). Empty text is rejected except for `query`, which explicitly allows an empty string (119-123). Limits are:

- `run_id`: 128 characters;
- `profile_id`: 128;
- `source`: 256;
- `query`: 2000.

These limits are Python character counts, not UTF-8 byte counts.

### Status and timestamps

`status` must be exactly `COMPLETED`, `FAILED`, or `CANCELLED` (103-105).

`started_at` and `finished_at` must be strings no longer than 80 characters, parse through `datetime.fromisoformat` after replacing `Z` with `+00:00`, and include a real timezone offset (43-52). They are normalized to UTC ISO strings with microseconds. A normalized finish earlier than start raises `SEARCH_HISTORY_TIME_RANGE_INVALID` (107-110).

### Counters and errors

`results`, `new_jobs`, and `duplicates` may be `None` (unknown) or an exact `int` from zero through 1,000,000,000 inclusive (27, 91-96, 127-129). Booleans and floats are rejected. Tests distinguish unknown `None` from measured zero and reject `True`, negative, float, and string counters (`tests/unit/test_search_history_service.py` lines 144-166, 191-196).

`errors` must be a list of at most 50 strings; each stripped string may be at most 1000 characters (112-116). Empty strings are not explicitly rejected. The normalized output strips each error (130).

### Filter JSON and secret-key guard

`filters` must be a dictionary accepted by `_validate_json` (55-88):

- maximum recursive depth is six, counting the root as depth zero (56-57);
- `None`, strings, booleans, exact integers, and finite floats are supported (58-61);
- strings are at most 2000 characters;
- lists and dictionaries contain at most 100 entries each (62-73);
- dictionary keys are nonempty strings up to 128 characters;
- key names matching `authorization`, `cookie`, `password`, `secret`, `token`, or `credential` case-insensitively are rejected, except exact key `excluded_work_authorization_flags` (29-30, 68-71);
- unsupported objects and non-finite floats are rejected.

The validated dictionary is compact-encoded with `ensure_ascii=False` and `allow_nan=False`; UTF-8 size must not exceed 16,384 bytes (77-88). Encoding then decoding produces a detached JSON-compatible copy. Unit tests cover secret-like `api_token` rejection and the explicit work-authorization exception (`tests/unit/test_search_history_service.py` lines 127-132, 176-183).

The filter guard checks key names, not arbitrary secret values. Error strings are length-checked and stripped, not redacted; callers must supply safe diagnostic content (source lines 68-71,112-116,130). This is guidance for callers, not an additional service guarantee.

## Public entry point: `record_search_history(store, value, *, connection=None)`

**Definition and docstring:** source lines 134-178.

### Inputs and validation order

- `store`: must provide `profile`, `engine`, and an audit-table-compatible persistence model.
- `value`: exact record dictionary described above.
- `connection`: optional active SQLAlchemy connection/transaction. When omitted, the function starts `store.engine.begin()`; when supplied, it participates in the caller's transaction (168-172).

The record is fully normalized before profile lookup or persistence (140). `store.profile(profile_id)` then requires the profile to exist (141); its exception is not translated here.

Action is derived from normalized status (142). Audit ID is deterministic:

```text
identity("search-history", profile_id, run_id)
```

(143). The persisted audit row uses profile ID as entity, normalized run as payload, and normalized finish time as timestamp (144-150).

### Nested `persist(target)` helper

The undocumented nested helper selects an insert implementation from `target.dialect.name` (152-158):

- `sqlite` -> SQLAlchemy SQLite insert;
- `postgresql` -> SQLAlchemy PostgreSQL insert;
- anything else -> `ValueError("SEARCH_HISTORY_STORAGE_UNSUPPORTED")`.

It inserts the deterministic audit row with `ON CONFLICT DO NOTHING` on audit ID, then reads that exact ID's action and payload (159-166). Thus retries do not overwrite the original.

After persistence:

- no readable row raises `RuntimeError("SEARCH_HISTORY_WRITE_UNCONFIRMED")` (174-175);
- a row with different action or payload raises `ValueError("SEARCH_HISTORY_RUN_CONFLICT")` (176-177);
- an exact match returns the normalized run (178).

The tests assert exact replay idempotency and changed replay preservation (test lines 109-119). The injected-connection test raises after recording and observes no history, demonstrating participation in the caller's rollback (`tests/unit/test_search_history_service.py` lines 168-174). These tests were inspected, not run for this reference.

### Side effects and errors

This is the write entry point. It inserts one terminal audit receipt on first success. Validation, profile lookup, unsupported dialect, SQL/database failures, unconfirmed reads, and replay conflicts propagate with the errors described above. With a caller-supplied connection, commit/rollback ownership stays with the caller.

## Cursor format

`_encode_cursor(profile_id, timestamp, event_id)` creates compact sorted-key JSON with exactly `profile_id`, `timestamp`, and `id`, UTF-8 encodes it, then URL-safe Base64 encodes without padding (source lines 181-186).

`_decode_cursor(value, profile_id)` requires a string of 1..512 characters, restores padding, uses validated URL-safe Base64, parses JSON, and requires exactly those three keys (189-207). It additionally requires:

- embedded profile ID equals the requested profile;
- timestamp and ID are strings;
- ID is exactly 64 hexadecimal characters;
- timestamp passes the same aware-time normalization.

Any handled Base64, Unicode, JSON, key, type, or value failure becomes `ValueError("SEARCH_HISTORY_CURSOR_INVALID")` (204-206). The cursor is profile-bound and opaque to callers, but it is Base64-encoded JSON, not encryption or a signature. Do not place secrets in cursor fields.

## Public entry point: `list_search_history(store, profile_id, limit=50, cursor=None)`

**Definition and docstring:** source lines 210-243.

### Inputs

`profile_id` is normalized as a nonempty, control-free string up to 128 characters (214). `limit` must be an exact integer from 1 through 100; booleans are rejected (215-216). `store.profile(profile_id)` requires the requested profile to exist before querying history (217).

When supplied, `cursor` must decode for this same profile (223-228). A cursor from another profile is invalid, as asserted in `tests/unit/test_search_history_service.py` lines 79-95.

### Query and ordering

The SQL query selects audit rows whose entity ID is the profile and action is one of the three terminal search actions (219-222). Results are ordered newest-first by timestamp, then audit ID descending (229-231).

Keyset continuation selects rows whose timestamp is older than the cursor timestamp, or whose timestamp is equal and ID is lexically smaller (223-228). The query fetches `limit + 1` to detect another page (229-234).

### Output

The function returns:

```text
{
  "profile_id": <normalized profile ID>,
  "runs": [<stored payload dictionaries>],
  "next_cursor": <opaque string or null>
}
```

(236-243). The extra row is not returned. `next_cursor` is generated from the final returned row only when another row exists (239-242). Stored payloads are shallow-copied with `dict(...)` but not re-run through `_normalize_run` on read (238). Corrupt legacy payloads can therefore surface native conversion/downstream errors.

Unit tests cover newest-first two-page traversal, profile isolation, cursor binding, durable reopen, cancellation, and empty history (`tests/unit/test_search_history_service.py` lines 63-107, 121-125, 185-189).

## API and worker integration

`apps/api/search_history.py` lines 66-112 exposes GET `/profiles/{profile_id}/search-history`, defaults limit to 50, caps it at 100, validates response scope, and projects service `run_id` to API item `id`. It maps missing profile to 404 and service cursor/limit errors to 422 (77-105). API integration tests verify read-only/no-network paging, profile isolation, nullable metrics, and fail-closed scope mismatch (`tests/integration/test_search_history_api.py` lines 118-215).

`services/discovery/search_history_events.py` lines 48-140 builds record inputs only from terminal `DISCOVER` operations, uses operation ID plus attempt number for run identity, maps blocked/dead-lettered states to failed history, and preserves missing counts as `None` (8-14, 48-58, 60-129). `workers/tasks.py` calls `record_search_history` during terminal worker handling; this reference does not claim that worker flow was executed.

## Safe fixture example (illustrative; not executed)

```python
from services.discovery.search_history import record_search_history, list_search_history

run = record_search_history(fixture_store, {
    "run_id": "synthetic-run-1",
    "profile_id": "fixture",
    "source": "synthetic-source",
    "query": "systems administrator",
    "filters": {"remote": "REQUIRED"},
    "started_at": "2026-10-09T10:00:00Z",
    "finished_at": "2026-10-09T10:01:00Z",
    "status": "COMPLETED",
    "results": 1,
    "new_jobs": 1,
    "duplicates": None,
    "errors": [],
})
page = list_search_history(fixture_store, "fixture", limit=1)
assert page["runs"] == [run]
```

Use only an isolated migrated SQLite/PostgreSQL fixture Store containing the synthetic profile. This example writes one audit receipt but performs no network/provider call or job application action. The unit fixture pattern appears at `tests/unit/test_search_history_service.py` lines 26-54.

## Maintainer cautions

- Preserve `None` for unknown counts; do not infer duplicates or convert unknown to zero.
- Keep exact replay idempotent and changed replay fail-closed; never rewrite the original receipt.
- Treat cursors as opaque, profile-bound pagination state, not authorization or secrecy.
- Keep the sensitive-filter key guard and JSON/byte bounds when extending filter fields.
- Historical evidence for another commit does not certify this immutable blob; runtime remains `NOT_RUN` here.

## References

All line ranges refer to source commit `b73750e37c487a008dfdd2277440a2559d98ecf4`.

- `services/discovery/search_history.py` lines 1-246
- `services/discovery/search_history_events.py` lines 1-143
- `apps/api/search_history.py` lines 1-115
- `tests/unit/test_search_history_service.py` lines 26-200
- `tests/integration/test_search_history_api.py` lines 22-219
- `apps/web/src/search-history-panel.tsx` lines 109-190
- `docs/evidence/2026-10-09-search-history.json` lines 1-52
