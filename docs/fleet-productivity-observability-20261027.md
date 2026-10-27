# Productive fleet operation and observability — 27 October 2026 UTC

## Delivered outcome

This continues the user's interrupted request to make Startup actually begin work, keep all existing workers usefully occupied, explain legitimate waits and provide detailed visual reporting. The implementation is installed on the workstation and its sanitized reporting is available through the existing owner-protected hub.

On re-entry at **2026-10-27T00:16Z**, all **31 runtimes were healthy**, but both automatic dispatchers were off. The earlier interrupted deployment had intentionally paused dispatch and had not re-enabled it. The existing policy also targeted only 12 active turns, two deliveries per minute, with productive discovery disabled. This was not an empty provider allowance or a missing fleet.

The explicit protected Startup submitted in this continuation completed at **00:33:31.193Z** as `ready_dispatching`. All 31 targets were verified; no new agent or conversation restart was needed. The user-authorized saved policy is:

| Setting | Saved value |
| --- | --- |
| Concurrent turn target | 31 current runtimes |
| Explicit worker reserves | 0 |
| Productive maintenance discovery | Enabled |
| Scheduler interval | 30 seconds |
| Maximum dispatch batch | 4 |
| Minimum completed-turn settling | 60 seconds |
| Stop guard | 7%, five-second polling, acknowledged |
| New-work quota brake | 15% |
| Resource envelope | Existing 6 GiB free-RAM floor, 25 GiB disk floor, CPU pacing at 92%, two heavy-test lanes |

At **00:33:45Z**, four agents were executing and 27 had queued work. At **00:35:44Z**, thirteen agents were executing, fourteen were queued, three were settling and one was waiting for a changed outcome. Twelve distinct maintenance tasks had been submitted. Quota was 93%, with no dispatcher hold and the guard armed/acknowledged. These are native activity observations, not an assertion that all workers execute at every instant.

## Scheduling useful work without manufacturing activity

Priority issue/file reservations, concrete CI remediation and exact-head independent reviews retain precedence. When those do not supply a worker with a new action, the fallback draws from **219 deterministic documentation opportunities** found in actual public definitions without docstrings at main revision `2f01b876762fcbdd70f11e69865a48257503af93`.

A missing docstring is explicitly an opportunity, **not proof of a defect**. The worker reads the exact immutable source and adjacent tests/documentation. It produces a bounded maintainer reference with real line citations, interfaces, dependencies, error behavior and examples, or reports `no_new_gap` when equivalent documentation already exists. No fabricated bugs, repeat CI runs, duplicate tests or filler are requested.

Each source blob is globally reserved once, including across restarts; each worker has at most one outstanding maintenance assignment. The original issue/PR ownership is preserved. Exclusive draft paths are outside the worker Git checkout, under the private productive-work artifact directory. No fallback instruction creates a PR, commits, merges, changes production files or starts extra agents. The primary receives pointers to completed drafts for review. `artifact_ready` means a structured draft receipt exists and basic source/scope checks passed; it does **not** mean independently accepted documentation.

Unknown native delivery outcomes remain fenced and are never blindly repeated. An exhausted evidence catalog is reported as awaiting useful work rather than causing invented tasks. The source catalog can be rebuilt with `Build-Productive-Catalog.py`; unchanged blobs remain deduplicated. The initial automated discovery category is documentation, not unrestricted autonomous rewriting of the repository.

## Workflow states and estimates

Native execution is separate from work scheduling. Labels include Executing, Queued for work, Assignment submitted, Turn settling, Waiting for CI, Waiting for a change, On-call reserve, Delivery needs review, Awaiting task receipt, Resource-limited, Quota protection, Needs a decision, Stopped and Dispatch paused.

Only native active turns count as executing. An explicitly reserved reviewer remains nonexecuting; reserve count is currently zero. A queued task has a real source/issue or review identity, queue position, conditional start estimate and next scheduler evaluation. Pending deliveries count against concurrency before activity is observed. CPU throttling reduces the effective dispatch batch and changes ETA pacing. A full capacity pool or unknown prerequisite results in an unavailable job-start estimate, not a made-up completion time. Future or stale plans cannot establish current queue readiness.

The local policy panel now targets an actual page element. The interrupted implementation had attached it to nonexistent `sustainSection`, so no controls appeared. **Use all current workers** stages the live count and zero reserves without starting work; **Save capacity policy** persists it. Protected Startup defaults to **Start working after verification**, with a restore-only opt-out. Its existing quota checks, idempotent request IDs, cancellation and uncertain-outcome handling remain. It waits for stable native readiness before enabling Sustained. An old successful startup is not treated as proof that dispatch is still on later.

## Visual reporting

**Fleet Atlas:** `https://m-techlabs.com/mission-control/analytics/`

**Grafana:** `https://m-techlabs.com/mission-control/grafana/`

Both views use the same sanitized native observations. Atlas provides live execution/queue scorecards, token flow, a workflow-state allocation chart, a 31-agent completed-turn occupancy heatmap, maintenance pipeline, observed model usage, task/ETA flight board and clickable per-agent turn ledger. The mobile drawer was tested for every current agent. Grafana has **18 data-backed panels**, with an agent selector, workflow-state history, turn wall-time percentiles, tokens/cache measurements, dispatch and freshness receipts.

The collector reads AO SQLite in query-only mode and maintains its own seven-day cache. Public statistics cover the last 24 hours and refresh every 30 seconds. Data older than 90 seconds expires in the frontend and Grafana queries. Token totals use native last-event measurements rather than repeatedly summing cumulative conversation counters; inspected events matched their native `last_token_usage`, and duplicate native source-event keys were absent in the inspected window.

Per-turn metrics include recorded start/end, wall time, state, interface, observed models and token fields only where the native conversation/time interval uniquely attributes them. Ambiguous or absent attribution stays unavailable, not zero. Historical occupancy unions completed intervals; a long-open prompt is not counted as continuous execution. Token use is not a productivity score, accepted work or a billing amount. Internal provider subagents lacking an AO identity are not fabricated as separately measured workers.

No prompt, response, native conversation identifier, credential, raw terminal text or absolute private artifact path is exported. The Grafana data source is restricted to the internal sanitized-data host. Grafana is loopback-bound and provisioned as Viewer for the existing protected hub, with separate local-file admin credentials excluded from Git. Container CPU/memory limits and persistent storage are configured. The Limited collector sign-in task was installed and verified; both containers use restart policies. The PC, Docker Desktop, hub and signed-in collector account must be available for fresh remote reporting.

## Safety and validation evidence

| Executed check | Result |
| --- | --- |
| Source regression runner | 45/45 groups passed, zero failed/unexecuted groups, stable source |
| Installed regression runner | 45/45 groups passed |
| Isolated real-browser controls | 38 scenarios passed; 35 inert fixture POSTs, zero production requests/native commands/JavaScript exceptions |
| Python telemetry/dashboard/proxy tests | 38 tests + 31 subtests passed |
| Ruff on new Python collector/catalog/dashboard generator/tests | Passed |
| Live Atlas/Grafana/local UI | All 31 Atlas agent drawers opened; desktop/mobile layout passed; local productive controls visible; zero mutating requests or JavaScript errors |
| Actual Grafana data queries | 18 panels queried successfully with frames and no query errors; legitimately empty selected-turn tables are not invented |
| Canonical deployment | 135 installed Fleet files/assets byte-compared against source; no native agents stopped |
| Analytics task installer | Limited task installation completed with exit zero; no agent commands |
| Protected Startup | Successful explicit start-work completion; subsequent native execution and fresh dispatch observed |
| External unauthenticated access | Atlas HTML/data and Grafana HTML/search redirected to Cloudflare Access login |

The deploy completed at **00:32:06.083607Z**. Live UI checks completed at **00:32:48.918Z**. Source fingerprint:

```text
828a51bc2a8221cd4f18ca9c7218d5b81a96b9b76559113cb3ae9593f1f289ba
```

The resumed work first preserved all interrupted source in an owned backup and scoped stash, fast-forwarded to `057fc55b1f6aa9a5391a51912a9fd18ab85c0385`, and reconciled the browser fixture while preserving its newer safe-argument/injection check. Existing lifecycle revalidation and fail-closed identity fixes were not overwritten. The retained recovery copy is private, not a source artifact.

The additional native exited-host probe only sends AO's read-only status request. It can distinguish a verified exited provider from its still-present terminal host without falsifying process counts or killing the host. Exact generation, listener ownership, stable process/registry observations and absent provider children remain required. That path has fixture coverage; this continuation's successful startup used already healthy runtimes and did not require a live stopped-host recovery.

## Remaining boundaries

Remote fleet commands remain disabled. This delivery provides online reporting and local guarded control, not a new Internet command API. The hub's Access policy was checked as owner-only with no everyone/bypass rule, and its tunnel was healthy. A signed-in external browser session was **not** independently exercised.

Native AO configuration-override and hook-trust warnings were not suppressed or removed. No unrelated process, BIOS/driver/network configuration, host reboot, credential or protected-main setting was changed. No additional agents were created. Maintenance outputs still need review; accepted merges and passing application CI are separate from utilization. No promise of permanent 100% activity or a guaranteed job-start time is made.

Operational source, setup and recovery instructions are in `tools/fleet_control/observability/README.md`. Hosted CI must be checked on the eventual published head; local tests and earlier-head checks do not substitute for new-head verification. Keep PR #825's existing protected integration and draft state unless separately authorized.
