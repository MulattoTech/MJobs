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
