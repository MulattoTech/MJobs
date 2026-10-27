# Maintainer reference: durable multi-page application state

**Pinned source:** `services/applications/multipage_state.py`
**Source commit:** `2f01b876762fcbdd70f11e69865a48257503af93`
**Source blob:** `e2f527688faf7f1b67c5c0871c3a1a50f5342b5d`
**Status:** Draft based on pinned source, API caller, tests, and existing guides. No tests were run for this note; it does not claim acceptance or runtime verification.

## What this module does

`multipage_state.py` stores durable, profile-scoped application-page progress and guarded navigation checkpoints. It is state persistence/reconciliation, not browser automation or an employer submission operation. The API contract keeps `manual_intervention_required: true`, and review is not submission (source `load_multipage_state`, lines 221–248; API description/routes at `apps/api/application_multipage_state.py`, lines 83–84, 91–144; integration assertions at `tests/integration/test_application_multipage_state_api.py`, lines 64–75, 133–154).

The implementation uses SQLAlchemy queries against persistence models and `Store` identity/time helpers; Workday behavior delegates page classification, transition decisions, route digest/origin rules to `workday_navigation` (imports/constants, source lines 4–30). Snapshots are appended to `audit` under `MULTIPAGE_STATE_SNAPSHOT`, keyed by profile/application/request identities, then the latest version is read back (lines 48–54, 211–219, 458–478).

## Entry points

### Read current state

```python
state = load_multipage_state(store, profile_id, application_id)
```

The function verifies the application belongs to the requested profile and that the profile exists. If there is no snapshot, it returns an empty `IN_PROGRESS` representation: empty order/progress, `current_page_id=None`, `version=0`, and manual intervention required. Otherwise it returns the latest state snapshot, including Workday navigation evidence when present (source lines 221–262). A foreign/missing profile/application raises `MultipageStateError` with code `APPLICATION_NOT_FOUND` (lines 251–262).

### Apply a generic page action

```python
result = transition_multipage_state(store, profile_id, application_id, {
    "request_id": "demo-save-001",
    "action": "SAVE_PROGRESS",
    "expected_version": 0,
    "page_id": "identity",
    "page_order": ["identity", "experience"],
    "progress": {"identity": {"review_state": "locally checked"}},
})
```

This is a local synthetic example only. `request_id` and page IDs must use stable allowed identifiers. Payload keys are closed to the supported set. Actions are `SAVE_PROGRESS`, `NEXT`, `PREVIOUS`, and `REVIEW`; `expected_version` must be a nonnegative integer. The first write must be `SAVE_PROGRESS` at version 0 and initialize `page_order` (1–100 unique stable page IDs); subsequent transitions must follow adjacent pages, and `REVIEW` is available only from the final page. Navigation-field combinations must match the selected action (source lines 21–30, 56–60, 146–196, 265–404).

Every action includes a `progress` object keyed by known page ID; values must be JSON-serializable and total canonical UTF-8 JSON size cannot exceed 256 KiB. The service rejects keys matching password/passcode/credential/cookie/session/token/secret, recursively through objects and lists (lines 21–25, 63–89). **This is a key-name rejection, not a value scrubber or PII anonymizer:** progress values are persisted in the audit snapshot with the request (lines 471–478). Use synthetic or intentionally reviewed local progress; do not store credentials, cookies, session tokens, or data that should not be retained.

The generic transition rejects raw caller-supplied Workday navigation evidence. Workday routes require the dedicated observation-confirming path (lines 407–415, 429–456).

### Workday observation-backed checkpoint

Use `initialize_workday_multipage_state(store, profile_id, application_id, observation, request_id=..., progress=...)` for a first confirmed page, and `transition_workday_multipage_state(..., prior_state, previous_observation, current_observation, intended_action, request_id=..., progress=...)` for later transitions (source lines 498–538 and 541–688). Observations are passed to `classify_step`; route origin/route digest, route ID, workspace mode, fingerprints, and visited-fingerprint path are checked against durable state before a transition is persisted. Calls that cannot prove a safe transition return a decision such as `MANUAL_REVIEW` or `REFRESH_AND_RECLASSIFY` with `state=None`, rather than advancing (lines 502–538, 546–575, 582–688).

A safer public route is the API’s Workday recovery endpoint, which delegates to `reconcile_workday_checkpoint` and forces `execute=False` plus `manual_intervention_required=True` (API lines 119–142; `services/applications/workday_recovery.py`, lines 46–84). It uses durable state rather than caller-supplied prior state and requires current workspace mode and expected version.

## Persistence, retries, and conflicts

Writes use a transaction and row locks for profile/application and route; SQLite explicitly begins an immediate transaction. Terminal application states (`SUBMITTING`, `CONFIRMED`, `SUBMISSION_UNCONFIRMED`) are not editable. Each operation is idempotency-keyed by profile/application/request ID: an exact retry returns the original snapshot; reuse with different request content raises `MULTIPAGE_REQUEST_ID_CONFLICT`. A stale `expected_version` raises `MULTIPAGE_VERSION_CONFLICT` (source lines 418–478).

On the HTTP routes, GET/POST live at `/profiles/{profile_id}/applications/{application_id}/multipage-state`; the Workday recovery POST adds `/workday`. The API validates strict request models, returns 404 for missing/foreign application state, 409 for version/idempotency/editability and Workday concurrency/confirmation failures, and 422 for malformed actions/progress/transitions (API lines 19–80, 91–142). Service errors carry a stable `.code`; callers should branch on code/status, not parse human-readable message text (source lines 33–46; API lines 52–80).

## Failure behavior and safety checks

- Invalid payload shape/action/version/page/order/progress fails with `MultipageStateError` codes such as `INVALID_REQUEST`, `INVALID_ACTION`, `INVALID_VERSION`, `INVALID_PAGE_ORDER`, and `INVALID_PROGRESS` (source lines 33–46, 146–196).
- Invalid ownership, or a no-longer-editable application, fails closed rather than returning another profile’s progress (lines 199–208, 251–262, 425–427).
- Wrong or stale version and changed idempotency payload are conflicts; transaction writes happen only after validation (lines 458–478).
- Workday state requires confirmed observation evidence and route/workspace binding; generic POST cannot assert it. Missing/stale/mismatched evidence maps to review/conflict outcomes (lines 407–415, 434–456, 541–688).
- The response is a saved local checkpoint; it does not mean an employer page was navigated, a form was submitted, or a submission was confirmed.

## Existing docs and verification scope

README and `docs/USER_TESTING.md` discuss the local mock application and emphasize that nothing is sent to a real employer, but do not explain this service’s contract, version/idempotency model, data retention/secret-key limits, or Workday route evidence. This maintainer note therefore fills a code-level documentation gap without duplicating those broader user instructions.

Relevant pinned test coverage (source paths below): unit cases prove reload and legal navigation, stale-version and invalid-transition atomic rejection, idempotent replay/conflict, no submit action, profile isolation, secret-key rejection, and Workday stale/route cases. Integration API tests exercise the route contract, restart readback, owner isolation, stale data, and reject unknown submit fields. These are inspected tests, not executed by this documentation task.

## References at the pinned commit

- `services/applications/multipage_state.py` — limits/IDs/errors/dependencies 1–46; validation and progress sanitation 56–196; profile ownership/read response 199–262; legal transitions 265–404; generic action/idempotency/transaction 407–478; Workday setup and observation path 481–688.
- `apps/api/application_multipage_state.py` — strict payload models/error mapping/API paths 19–80; GET/POST/Workday route handling 83–144.
- `services/applications/workday_recovery.py` — recovery boundary and non-execution result 27–84.
- `tests/unit/test_multipage_application_state.py` — persistence/retry/transition/ownership/secrets/Workday negatives 91–217, 219–408.
- `tests/unit/test_fleet_t03_workday_progress.py` — current-observation, resume, route/workspace binding cases 67–209.
- `tests/integration/test_application_multipage_state_api.py` — API success/reload/idempotency and rejection cases 55–243.
- `tests/integration/test_fleet_t03_workday_recovery.py` — connected checkpoint/restart and stale/route failures 70–152.
- `README.md` — responsible-use boundary 19–20, 113–117; `docs/USER_TESTING.md` — manual real-employer handling 100–104. These do not document this module’s maintainer contract.
