# Mock ATS Cancellation Boundaries

## Objective

Advance open P0 issue #5 / MJ-REQ-004 by making durable cancellation and deadline checks effective at the local Playwright adapter's irreversible submission boundary. Preserve the no-blind-retry rule and keep external submission disabled.

## Acceptance behavior

- Cancellation/deadline state is checked after bounded page navigation and form preparation waits.
- The operation deadline is checked after the durable `SUBMITTING` reservation and before the browser's Submit click. The API already rejects cancellation once this reservation exists and requires reconciliation.
- Deadline expiry in that gap prevents the click. Because the persisted state already crossed the submission reservation boundary, reconciliation records `SUBMISSION_UNCONFIRMED` if no receipt is found; a repeated call does not blindly resubmit.
- A successful mock submission still creates exactly one receipt, while an ambiguous post-click failure remains `SUBMISSION_UNCONFIRMED`.
- Browser errors retain fixed application/operation classifications; raw page content, screenshots, or browser diagnostics are not persisted.

## Implementation and validation

1. Run a local Chromium regression that expires only once the application is durably `SUBMITTING`; verify the current flow fails to observe the deadline before Submit. Confirm accepted cancellation before reservation remains covered by worker lifecycle tests.
2. Add checks at navigation/form-stage boundaries and immediately before the irreversible submit click.
3. Run all mock application browser tests, worker lifecycle/operation tests, relevant application API tests, and the full local Python/browser suite.
4. Verify normal one-receipt submission, CAPTCHA intervention, cancellation before click, and ambiguous receipt/no-retry history.
5. Record exact candidate hashes, UTC times, results, failures/skips and limitations. Keep issue #5 OPEN and MJ-REQ-004 PARTIAL.

## Boundaries

Playwright's existing navigation/default action timeouts remain the per-call wall bounds. This slice adds cooperative checks between blocking calls; it does not cancel a synchronous Playwright call mid-call or introduce any real-ATS adapter. External application submission stays OFF.
