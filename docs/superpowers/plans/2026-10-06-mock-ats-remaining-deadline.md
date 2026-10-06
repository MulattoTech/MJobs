# Mock ATS Remaining-Deadline Plan

**Goal:** Bound every mock ATS browser action by the durable operation's remaining monotonic budget.

**Architecture:** The worker's existing cancellation/deadline check will expose its remaining time. The mock application adapter will refresh Playwright's per-action timeout from that value before each waiting browser action, while retaining current local-only submission gates and reconciliation.

**Tech Stack:** Python, Playwright sync API, unittest.

**Spec:** `docs/spec/areas/governance_safety.md` (MJ-REQ-004), GitHub issue #5.

## Global Constraints

- External application submission remains OFF; this change only affects the configured fictional mock ATS.
- CAPTCHA/intervention gates and the pre-submit review/policy reservation remain required.
- A post-submit ambiguity remains `SUBMISSION_UNCONFIRMED`; never retry blindly.
- The worker's operation timeout and cancellation check remain the timing authority.

## Review Focus

- A navigation or selector action must not keep using its original fixed timeout after the worker budget shrinks.
- The final submit click and confirmation wait must be limited by the remaining budget; timeout afterward must reconcile the durable submission state.
- Test helpers without a worker deadline must retain their existing bounded Playwright action timeout.
- A deadline already expired before an action must stop before that action.

### Task 1: Budget mock ATS browser actions

**Files:**
- Modify: `workers/tasks.py`
- Modify: `services/applications/mock_adapter.py`
- Test: `tests/unit/test_mock_application_deadline.py`
- Test: `tests/browser/test_mock_application.py`

**Interfaces:**
- The callable worker `check` exposes `remaining_seconds() -> float`, using the same monotonic deadline and cancellation check as `check()`.
- Add a mock-adapter helper that computes `min(action_max_ms, floor(remaining_seconds * 1000))`; checks without the optional deadline provider retain the existing maximum, and expired/invalid budgets fail closed.

**Steps:**

- [x] Add a failing browser test proving the real mock ATS flow uses a short supplied worker budget while preserving its existing submit/reconciliation behavior.
- [x] Add a failing worker integration test proving the callback exposes remaining monotonic time.
- [x] Implement the worker remaining-time accessor and refresh the page default or explicit timeout immediately before each Playwright action that can wait, including Submit and confirmation.
- [x] Run the mock application browser and worker lifecycle suites; verify the expired-before-submit test still leaves no receipt and `SUBMISSION_UNCONFIRMED`.
- [x] Run the full local suite and repository validators.
- [ ] Record evidence, keep issue #5 / MJ-REQ-004 PARTIAL, and integrate by local-only non-force fast-forward.

**Acceptance:** No Playwright action receives a timeout greater than the worker's remaining budget or the existing per-action maximum; an expired pre-click budget produces no submit request; an ambiguous post-click state follows existing receipt reconciliation without an automatic retry.
