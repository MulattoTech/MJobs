# Desktop activity notifications

Requirement: **MJ-REQ-204.N05** (issue #366); parent **MJ-REQ-204**.

Desktop delivery runs in the user's browser. It is separate from the in-app activity list and from server notification workers such as ntfy. The browser implementation is `apps/web/src/desktop-notification-provider.ts`, with controls in `desktop-notifications.tsx`. The common server-provider catalogue may describe this execution surface; a server process cannot display a notification on the user's desktop by itself.

## Enable and disable

1. An operator enables the existing `MJOBS_NOTIFICATIONS_ENABLED=1` setting for the MJobs instance. Its default remains off. Follow the normal deployment process for any environment change.
2. Open MJobs in a supported desktop browser using HTTPS or a trustworthy localhost origin. Select the profile whose activity you want to follow.
3. In **Desktop notifications**, choose **Enable desktop notifications**. This user action requests the browser's notification permission when needed. Loading the page never requests permission.
4. Keep the MJobs page open. Use **Disable desktop notifications** to stop this profile's desktop delivery without changing browser-wide permission.

Browser permission and the MJobs profile preference are separate gates. Both must allow delivery. A denied permission must be changed through the browser's site settings; the application does not repeatedly prompt or bypass the decision. Availability also requires the Web Locks API and usable local storage for inter-tab coordination. Unsupported or unavailable states are explained in the controls.

The preference belongs to this browser origin and profile. It is not a server-wide preference, a cross-device subscription, or a mobile push registration. The application does not automatically subscribe another profile.

## Supported activity

Delivery consumes only successful, profile-bound snapshots from the existing read-only operation poll. The polling interval remains 15 seconds. Operation updates from other action handlers do not feed desktop delivery.

| Operation outcome | Fixed description |
| --- | --- |
| `COMPLETED` | completed |
| `FAILED` | needs attention |
| `DEAD_LETTERED` | needs review |
| `CANCELLED` | was cancelled |
| `BLOCKED` | needs review |

Known operation kinds receive fixed labels such as **Job discovery**, **Source collection**, **Company discovery**, **Document preparation**, **AI evaluation**, **Resume extraction**, **Resume comparison**, **Company source refresh**, **Application dry-run**, **Application form review**, and **Fixture application**. Unknown kinds use **Background task**. A worker completion is not presented as confirmation that a real application was submitted.

One newly observed outcome produces a fixed message such as “Job discovery completed.” Several new outcomes in the same poll produce one fixed summary asking the user to review Activity in MJobs. There is at most one notification attempt per processed poll batch.

## Privacy, replay and failure behavior

Notification titles and bodies contain fixed application text. They do not include profile names, job titles, employer names, résumé text, task errors, credentials, operation IDs or arbitrary source content. The replacement tag is opaque. Notifications are silent, and repeated tags do not request a new alert sound. Clicking a notification focuses the existing MJobs window.

The initial successful snapshot establishes a baseline, so opening or reloading the page does not replay historical activity. Explicit enabling establishes a fresh baseline as well. Failed activity reads pause delivery; recovery establishes the next reliable baseline before future new outcomes can trigger notifications. A profile switch discards the previous profile's browser delivery state.

Same-origin tabs serialize receipt checks and updates with Web Locks. A profile's bounded local record remembers the last 512 outcome identities, using operation ID, state and update timestamp. The receipt is stored before attempting native delivery. Failed or uncertain attempts are not automatically retried in a notification loop. A corrupt or unsupported preference record does not enable delivery. Unavailable storage or coordination prevents delivery rather than allowing an unsafe duplicate election.

If saving Disable fails, this tab keeps delivery suppressed for that profile across focus changes and profile remounts until an explicit successful Enable. Other tabs or a page reload cannot inherit an unsaved preference change; the controls report that limitation.

These bounded receipts do not promise exactly-once delivery. An observation can be missed while a page is closed, a browser is suspended, reads fail, or an attempt fails. Browser/OS suppression and quiet-mode policies can prevent a visible toast. A successful native constructor handoff means the browser accepted an attempt; it does not prove that the operating system displayed it.

## Scope limits

This provider supports non-persistent desktop browser notifications while MJobs is open. It does not implement service-worker push, delivery after the page closes, mobile notification support, email, ntfy, Slack or Discord. It does not send data to an external notification service, modify job applications, or submit an application. No notification permission, provider access or browser safety control is bypassed.

The generic operation-outcome vocabulary is the implemented scope. The parent requirement's potential events, such as an excellent match or an authentication expiry, are not separately claimed merely because an operation finished. Server provider support and formal parent acceptance remain separate.

## Verification and evidence

The dated evidence file is `docs/evidence/2026-11-13-desktop-notifications-ca95.json`. It records the exact source, commands, results and remaining qualification. Consult that record before calling this provider accepted or deployed.

Reproducible focused checks from the repository root:

```text
npm --prefix apps/web test -- src/desktop-notification-provider.test.ts src/desktop-notifications.test.tsx --maxWorkers=1 --coverage.enabled=false
npm --prefix apps/web run build
python -m pytest -q tests/browser/test_desktop_notifications.py tests/browser/test_operation_notifications.py
```

The browser checks use an owned loopback API, synthetic SQLite profiles and operations, and the built application in Chromium. They exercise the native Notification constructor separately from deterministic provider/component tests. No third-party notification or real applicant data is used. A headless browser run does not certify a visibly rendered Windows toast.

API references: [Notifications API](https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API), [requestPermission](https://developer.mozilla.org/en-US/docs/Web/API/Notification/requestPermission_static), and [Web Locks API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Locks_API).
