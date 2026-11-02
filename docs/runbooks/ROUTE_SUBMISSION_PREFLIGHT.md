# Fresh route resolution before submission admission

For an otherwise admissible REAL application, `authorize_submission` attempts
lightweight public redirect resolution before claiming a submission through a
board or aggregator. The deployment's external-submission gate remains OFF.
This contract does not enable an employer adapter, grant consent or send an
application.

The first transaction checks the existing policy, application state, validated
documents and approved facts, durable job-specific intent, barriers and caps.
A disabled or ineligible request does not perform the route request. A route is
exempt only when its actual HTTPS destination matches current verified company
domain evidence or a qualified ATS tenant. A source/provider label alone does
not establish this exemption.

The resolver runs after the first transaction releases its locks. It reuses the
public-IP-pinned transport, robots policy, redirect restrictions, response caps,
operation cancellation and eight-second total resolver budget. Candidate URLs
must remain within verified employer domains or current qualified ATS tenants.

The second transaction repeats admission checks and recounts caps. The original
application, selected route, current profile version, document hashes, intent,
route authority and company evidence must still match. A changed or expired
snapshot, failed probe, cancellation or invalid result cannot consume intent.
An eight-second freshness check rejects an old result after database access is
regained; it is not a timeout on acquiring database locks.

`UNRESOLVED` means the bounded attempt completed without a verified destination.
It can proceed only while all existing submission gates still allow the exact
selected route. A resolved destination is evidence, and does not silently change
that selection. Blocked or malformed results deny admission for review.

The durable `APPLICATION_SUBMISSION_ROUTE_CHECKED` event retains fixed statuses,
reason codes and hashes. It omits raw source/destination URLs, query values,
response bodies and exception messages. Intent consumption remains atomic:
PostgreSQL uses `INSERT ... ON CONFLICT DO NOTHING RETURNING id` to distinguish
a newly consumed receipt from a conflict without assuming driver row-count
availability.

## Validation

Run `tests/integration/test_submission_route_preflight.py` with a disposable
`MJOBS_TEST_POSTGRES_URL` to exercise both SQLite and PostgreSQL. Supporting
regressions are `tests/integration/test_live_submission_intent_admission.py`,
`tests/integration/test_configured_submission_limits.py` and
`tests/unit/test_direct_employer_route_redirects.py`.

These tests use synthetic consent and provider fixtures. They cover no-probe
denials and first-party exemptions, actual persisted integrated-board evidence,
outside-lock requests, cancellation, stale documents/profile/evidence/intent,
recounted caps, durable sanitized readback, replay and concurrent claims.
No real employer submission is needed to verify this admission contract.

Database lock waits and the existing route-authority evidence join remain
outside a global operation-bound guarantee. MJ-REQ-004 therefore remains PARTIAL.
