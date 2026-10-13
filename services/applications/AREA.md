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

Controlled local DRY_RUN preparation exposes owner-scoped cancellation and an
explicit acknowledged retry action. Admission serializes on the application and
admits at most one active controlled run, while replaying an existing request
keeps its identity. Retrying a saved input set requires the retry action, current
reviewed inputs and a fresh request ID; its immutable `retry_of` link preserves
the earlier attempt. Cancellation uses the shared durable worker fence and never
claims a receipt after cancellation. A completed receipt stays unchanged.
If a process exits after saving verification but before final operation completion,
cancellation preserves that receipt; lease recovery reuses it without reopening
the browser. The mounted UI can recover/cancel an active run even when the current route no
longer supports preview. All browser work remains on the local product fixture;
no employer interaction or submission is enabled by these controls.

The mock Playwright flow rechecks the durable operation's monotonic deadline and
cancellation after navigation and upload stages, and checks the deadline again
after the SUBMITTING reservation but before the irreversible submit click. The
worker exposes remaining seconds from that same monotonic deadline; browser
launch, navigation, every waiting locator action, Submit, and confirmation wait
are capped by the smaller of the remaining budget and their existing action cap.
Questionnaire preflight and receipt HTTP reads use the same operation check, so
an active GET can close on cancellation or deadline. If receipt verification is
cut short after a SUBMITTING reservation, persist SUBMISSION_UNCONFIRMED and
never retry submission.
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
