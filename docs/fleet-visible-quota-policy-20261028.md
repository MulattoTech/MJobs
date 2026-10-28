# Visible configurable quota policies — 28 October 2026 UTC

The user's screenshot showed36/36 healthy runtimes, a failed Startup at13% quota, a hard-coded15% work brake and an accepted-request toast. The low quota was a policy hold, not a runtime-health failure.

## Delivered

MJobs now stores `minimumRemainingPercent` in its existing quota configuration. The absent-field migration retains the prior15% default. Startup, arming, runtime decoration, preflight, dispatch and guarded reconciliation use the same saved policy. Values must be whole percentages from7 to99 and cannot undercut the configured emergency threshold. A known blocked new Startup receives a rejected response before a new Startup record is accepted. The independent7% emergency monitor and ownership/approval protections are unchanged.

Both dashboards have a prominent **Start / new-work quota policy** panel above their operating controls. It shows remaining allowance, the saved pause boundary, a direct Save button, and the separate emergency guard. At equality new work is blocked: a12% limit permits13%, but a13% limit does not. Lowering a limit has a confirmation explaining that an already-enabled scheduler can resume. Missing/stale usage remains blocked. Saving does not create/resume agents or toggle a paused fleet.

BabyTracker updates only its own `minimumRemainingPercent`. Its displayed effective boundary also respects the read-only shared emergency threshold. Its AGY/manual-provider behavior and project isolation remain intact. New Codex work is independently marked blocked even when the overall project is paused or contains AGY agents.

The Startup quota badge and detail no longer call36/36 healthy runtimes a runtime failure, nor claim enabled dispatch is usable while a quota hold applies. An old quota-only Startup error stops obscuring the current policy once the hold is resolved. Remote status includes the configured limit and an allowlisted quota-block explanation; remote commands remain disabled.

## Executed evidence

- MJobs complete source regression suite: **50/50 groups passed**.
- Isolated real-browser controls: **39 scenarios passed**; no production requests.
- BabyTracker full suite: **92 tests passed**.
- Actual installed UI on both boards: both Save buttons exercised with their existing values; MJobs15%, BabyTracker7%. Exactly two configuration POSTs; no Startup/native agent request; no JavaScript errors.
- Installed code byte-compared with tested canonical source. Dashboard-only restart; all native agent identities/generations unchanged. Emergency configuration and Startup record bytes unchanged at deployment.
- Existing values were preserved, not silently lowered: **MJobs15%, BabyTracker7%, emergency7%**. At the observed13% remaining, MJobs is intentionally blocked until the user lowers its visible work limit or quota resets; BabyTracker quota permits work but its project remained paused.
- Live UI proof: `2026-10-28T03:35:14.381Z`. Deployment: `2026-10-28T03:27:40.189930+00:00`. Source fingerprint: `1625c7546281ecd9a69542f23ab6e67d88f8067c321aef792fedf8ecdd4c54ee`.

A standalone Windows ownership probe timed out on one pre-deployment regression pass; the independent rerun and final complete suite passed without modifying that ownership check. This change does not promise zero provider-metering overshoot, accepted product work, automatic quota reset/restart, or authenticated external browser verification.

Repeat: `node tools/fleet_control/check-fleet.js`; `node tools/fleet_control/test-browser-controls-isolated.js`; BabyTracker `node --test tools/fleet-control/test/*.test.js`.
