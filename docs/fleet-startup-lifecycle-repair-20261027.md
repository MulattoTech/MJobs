# Startup polling and cancellation repair — 27 October 2026

## Incident and reproduced causes

The user's interrupted Startup was `uncertain`, dispatch was off, and the primary's provider process had exited while its native terminal host remained alive. Independent read-only native probes verified 30 healthy targets and the exact stopped primary, with no conflicting primary or unresolved restore submission. The saved recovery manifest and session identities were intact.

Two controller defects were reproduced rather than bypassed:

1. `_wait` treated its own polling sleep as an adapter operation, with a deadline only 100 ms longer than the intended sleep. A delayed timer could produce `STAGE_TIMEOUT: fleet restoration poll interval completed after its deadline` even though the actual restoration deadline was minutes away. A deterministic test with a 500 ms clock advance reproduced that exact error and stranded the operation as uncertain.
2. `RestoreFleet.tick` could finish a read after `pause()` and then overwrite its in-memory phase, statistics and ready-target records. The durable state remained paused while memory could say `restoring_primary` with `active=false`. Exact-operation reconciliation correctly refused this inconsistent state. Barrier tests reproduced late writes after watchdog/session/runtime/identity/resource reads; the session-read failure path could also attempt to start AO after a pause.

## Repair

Polling is now an interruptible delay bounded by the real stage and overall deadlines, not an artificial microdeadline. Cancellation and owner/revision checks run before and after the pause. External adapter/native-action deadlines remain enforced; the action deadline remains 15 seconds. Read-only inspection has a 30-second budget because the real native inspection is multi-step; current-readiness verification has 90 seconds to establish its existing 15-second stable-health requirement. The overall and restoration limits remain unchanged.

Restore ticks bind to the exact state object/operation and recheck that it is still active after each awaited watchdog, session, runtime, identity and resource read. A paused or superseded observation returns without changing the paused state or launching anything. The existing immediate-before-native-submission guards and durable uncertain-submission records are retained.

No recovery JSON was manually cleared, no process count was falsified, and no native agent was killed, replaced or duplicated. Deployment restarted the dashboard service only, preserving the existing Startup/restore/snapshot/dispatch/emergency files byte-for-byte before restart. Reloading the saved paused operation removed the inconsistent transient in-memory phase.

## Executed validation

- Before repair: the jitter regression failed with the same poll-interval timeout shown in the user's screenshot.
- After repair: all 108 Startup tests passed, including delayed polling, actual stage expiry, cancellation, ownership loss, quota and emergency checks.
- Seven new barrier cases passed for pauses during watchdog, successful/failed session read, successful/failed native inventory read, identity inspection and resource inspection. They assert identical paused in-memory/durable state and zero native commands/AO launches.
- The complete source runner and installed runner each passed 47/47 groups, zero failed or unexecuted groups, with stable source.
- The isolated real-browser control suite passed all 38 scenarios at `2026-10-27T05:03:10.605Z`, using 35 inert fixture requests and zero production requests, native commands or JavaScript errors. This is UI routing/confirmation evidence, not a claim that live destructive actions were executed.
- Canonical tested-source fingerprint: `46abf8989f58a96ad1e59b0f89b0c3556f699495f600dc9885c09ecfc09d2709`. All deployed canonical files were byte-compared against that source. The installed directory also contains historical local diagnostic helpers, so its whole-directory fingerprint is distinct.
- Deployment completed at `2026-10-27T05:00:50.7479314Z`; 138 canonical files/assets matched the source and the control-state preservation check passed.
- Post-deployment read-only checks showed the durable and reported restore phase consistently `paused`, while current runtime inspection positively identified 30/31 healthy and the stopped primary.

One intermediate test attempt accidentally overlapped another full runner and collided in legacy fixed-name fixture directories. Those results were not counted as a pass. The superseded isolated test process was stopped, and a single full runner completed successfully before deployment; installed verification ran separately.

## Live execution boundary

This session's tool safety checks blocked both creation of a combined recovery/start script and a subsequent verification-only Recheck request. Those blocked calls were not executed. The session did not bypass the restriction or claim the fleet resumed. At the last post-deployment inspection, the original interrupted Startup still required its ordinary guarded Recheck, and the primary was still stopped.

On the local board, refresh the page, choose **Recheck safely**, then choose **Startup/Continue Startup**, leaving **Start working after verification** selected. Recheck must account for the exact saved run and all preserved targets before it permits a new explicit Startup. A successful start must end with current 31/31 runtime verification and enabled Sustained dispatch; an old saved success or an armed quota guard alone is not evidence of work.

The site's existing one-way status feed continues to reflect the local controller; remote command APIs were not added. Actual post-repair native restoration, dispatcher activation, destructive button execution, and signed-in external browser access remain NOT_RUN in this session. Hosted checks must be read on the published commit. Existing branch protection and PR draft state are preserved.
