# Fleet delivery reliability and quiet controls — 27 October 2026

## The observed failure

At 12:16 UTC, all31 MJobs runtimes were healthy but zero agents were executing. Sustained was enabled; the last reported successful dispatch was 10:01 UTC. A terminal-delivery verifier treated post-send activity as uncertain, without consulting the exact native task record. Read-only inspection found24 historical uncertain reservations with exact completed message receipts. The three All Hands sends in the user's screenshot were not silently promoted because their original message hashes did not match the recorded native requests.

The finite documentation catalog also had206 drafts awaiting review, with no automated independent-review queue. Merely relabeling idle agents or enlarging CPU/memory limits would not fix either problem. `main` last changed at02:41:43 UTC (21:41:43 Central on26 October); branch activity and private artifacts were not presented as shipped product changes.

## Delivered corrections

**Exact receipts, not an activity LED.** A read-only/query-only SQLite adapter binds the expected project, session creation, native conversation, runtime generation, database identity and pre-send turn cursor. It matches the complete task hash inside a bounded post-send time window. Only one matching native record can verify receipt; wrong identities, old/future records and ambiguous duplicates remain unverified. The original task text never appears in the API evidence. A received task is distinct from a completed turn and neither implies accepted work.

**Periodic reconciliation without replay.** When no controller mutation or dispatch cycle is active, the controller checks eligible uncertain reservations at most once per minute. A verified receipt updates only the matching reservation and scheduler hold. Duplicate-protection records are retained. Historical confirmations cannot roll the latest-success timestamp backward or erase an unrelated newer error. Unverified reservations are not automatically discarded when their time-to-live expires. No native message is resent by reconciliation.

**Shared pacing, not false failures.** The scheduler's60-second settling policy could select an agent before the shared sender's120-second interval expired. That denial could poison a new work reservation as uncertain. Sustained and All Hands now consult the same sender hold before reserving work. A real cooldown has a displayed next-eligible time; an unknown delivery shows verification pending rather than a fabricated ETA.

**A usable completed Startup panel.** Healthy already-restored fleets display a disabled **Already started** button. Preview and submit handlers reject the redundant action too. Routine health refreshes no longer flip the refresh button, reset the completed title or erase the progress bar. The last completed count is explicitly historical during a pending health check. Idle polling is reduced from every2seconds to every8seconds; active recovery still polls more quickly. All Hands no longer announces “dispatched” when it has zero verified submissions.

**Finite useful follow-on work.** Existing source-bound drafts can be independently reviewed by a different worker, once per draft hash, in exclusive private result files. Reviewers check factual/source accuracy and duplication; they may not alter the original draft, production code or PR ownership. Correct receipts release the review assignment but remain acceptance-unassessed. The primary receives changed review outcomes and must reserve scope and use ordinary protected PR integration for useful results. The queue is finite; no defects, commits, approvals or busywork are fabricated to make a utilization number green.

**Quiet analytics startup.** The visible Windows Terminal/PowerShell process was time-correlated to the directly launched Analytics Standard scheduled task. Its action now uses a hidden `wscript` wrapper, still under the same Limited account and singleton collector. The collector itself remains read-only. No unrelated terminal or agent was terminated, and no execution-policy bypass was added. The post-deployment window observation found no visible shell windows.

## Executed evidence

- Source and installed regression suites: **49/49 groups passed**, stable source.
- Isolated actual-browser controls: **39 scenarios passed**, zero production requests.
- Live shared-port browser: **31 agent drawers**, disabled duplicate Startup, unchanged completed title/progress through **12 repeated observations**, zero mutating requests or JavaScript exceptions.
- The query-only receipt reader confirmed24 exact historical completed requests before deployment; the periodic reconciler has recorded **15 distinct verified reservation transitions** at this checkpoint. Unknown outcomes were not cleared.
- Live checkpoint **2026-10-27T12:56:24.318816+00:00**: **9 native executing turns**, **31/31 verified runtimes**, Sustained enabled, quota **21%** with an armed/acknowledged **7%** stop guard. These values are observations, not a promise of continuous100% activity.
- At that checkpoint independent-review results: **71 ready**, **10 outstanding**. Results still require integration; drafts and reviews are not code commits or accepted features.
- Deployment fingerprint: `e5b4087bec7ebbe1f2d368e3ad80d4612f7bc945c98a92ab44318c96d1f2f280`. Canonical installed source was byte-compared, and native agents were not restarted.

## Scope and remaining boundaries

The working target remains31 and deliberate reserves0. Existing memory/disk protection, project/file ownership, provider policy,15% new-work brake and7% stop guard remain intact. Real approval decisions and genuinely ambiguous deliveries are still held; they are not auto-approved or erased. Destructive emergency controls were tested only against fixtures, not by killing the running fleet.

BabyTracker is a separate bounded controller at port4319. Its live roster contained **zero registered workers**; it was paused and does not implement worker provisioning. Inspection found no MJobs-style startup/reconciliation controller there. The proposed BabyTracker native-delivery/UI patch was rejected by the tool's safety check and was **not applied**. No infant records, production application data or BabyTracker runtime settings were changed. It is not claimed to be repaired or running.

The remote monitor remains read-only behind the existing Access policy. Existing status publishing is updated by normal deployment; authenticated external browser login was not retested. Publication is on the existing PR825 branch and is not a claim that main was merged or that all application CI passed. No standing PR approval, main protection or quota limit was changed.

## Repeatable checks

```text
node tools/fleet_control/check-fleet.js
node tools/fleet_control/test-browser-controls-isolated.js
node --test tools/fleet_control/test-native-delivery-receipts.js tools/fleet_control/test-artifact-review.js
git diff --check
```
