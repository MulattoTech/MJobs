# Mock application adapter

Own the explicit local mock ATS headless two-page flow, upload integrity,
validation, intervention stop, dry-run and receipt-confirmed mock submission.
Interface: apply_mock(store, application_id, mock_origin, output_dir, submit=False).
Requirements: MJ-REQ-121, 124, 139, 145, 146, 148, 158, 208, 213.
Depend on domain policy, persistence and Playwright. One worker/browser at a time.
All HTTP/browser requests are restricted to configured mock origin. No real ATS
submission is implemented or enabled. Persist SUBMITTING before click; failed
receipt lookup yields SUBMISSION_UNCONFIRMED and never a blind retry. Persistent
browser sessions reside only in private output storage. Test:
python -m unittest tests.browser.test_mock_application.

The mock Playwright flow rechecks the durable operation's monotonic deadline and
cancellation after navigation and upload stages, and checks the deadline again
after the SUBMITTING reservation but before the irreversible submit click. The
worker exposes remaining seconds from that same monotonic deadline; browser
launch, navigation, every waiting locator action, Submit, and confirmation wait
are capped by the smaller of the remaining budget and their existing action cap.
The cancel API rejects cancellation after the reservation and requests
reconciliation. A missing post-click receipt remains SUBMISSION_UNCONFIRMED;
the browser does not blindly retry.

## Shared ATS knowledge (MJ-REQ-084)

`knowledge.application_adapter_knowledge(connection, route_id, operation_mode=None)`
reads existing route/company ATS identities without network requests or detection.
Exact provider/tenant matches take precedence over company-only hints; ambiguous
or weak discovery evidence requires review. Same-name companies are never joined.
Application detail and route APIs expose current metadata; document preparation
records the selected route's knowledge in its existing immutable audit event.
Hints neither rewrite application URLs nor authorize execution. Only explicit
mock routes in a bound FIXTURE workspace advertise the installed mock adapter;
real providers report UNSUPPORTED and external submission stays disabled.
Tests: `python -m unittest tests.integration.test_application_knowledge -v`.
