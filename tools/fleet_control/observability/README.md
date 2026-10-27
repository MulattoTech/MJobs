# Fleet Atlas and Grafana

Read-only operational reporting for the existing AO fleet. This stack neither starts agents nor forwards Fleet Control or AO command APIs.

## Installed views

- Owner-protected Atlas: `https://m-techlabs.com/mission-control/analytics/`
- Owner-protected Grafana: `https://m-techlabs.com/mission-control/grafana/`
- Workstation Atlas: `http://127.0.0.1:20800/mission-control/analytics/`
- Workstation Grafana: `http://127.0.0.1:18731/mission-control/grafana/`

The polished Atlas overview has native-execution and queue scorecards, output-token flow, workflow-state allocation, completed-turn occupancy, maintenance artifacts, model usage, and a per-agent turn ledger. Grafana offers provisioned analytical panels and an agent selector over the same sanitized data.

## Data and interpretation

`collect-analytics.py` reads the local Fleet API and opens AO's SQLite database in query-only/read-only mode. It imports native usage events into an owned cache with a seven-day retention limit; the published view covers the last 24 hours. Collection runs every 30 seconds. Each page expires a feed after 90 seconds rather than presenting an old success as live.

Usage values are native per-event reports, not sums of cumulative conversation totals. Cached input is a subset of input, not an additional token category. Tokens indicate resources used, not accepted productivity or a bill. Per-turn token attribution is available only when a completed native conversation/time interval uniquely owns an event. Missing or overlapping attribution remains unavailable, not zero. Recorded turn occupancy is the union of completed intervals; an open prompt is not credited as continuous work. Internal provider subagents that lack a stable AO binding are not invented as separate workers.

Published fields exclude native conversation identifiers, prompts, responses, command lines, terminal output, credentials, and absolute private paths. Private drafts and result receipts stay in `C:\Tools\AO-Fleet-Control\artifacts\productive-work`; publishing their counts does not approve their content.

## Reproducible installation

Install the tested Fleet files under `C:\Tools\AO-Fleet-Control` first. The collector uses the existing MJobs Python environment, and its explicit installer registers a Limited, current-desktop-user sign-in task with a singleton mutex and no execution time limit:

```powershell
pwsh -NoProfile -File C:\Tools\AO-Fleet-Control\Install-Fleet-Analytics.ps1
```

Keep `observability/compose.yaml`, `data-nginx.conf`, `dashboards`, and `provisioning` together in the installed observability directory. Configure `FLEET_PUBLIC_ANALYTICS` to the sanitized published directory, not the AO database or a home directory. Supply an independently generated strong admin password in `secrets/grafana_admin_password`; do not commit or print it. `.env` and `secrets/` are excluded from Git. The existing Docker network is `mtechlabs-hub`.

```powershell
docker compose -f C:\Tools\AO-Fleet-Control\observability\compose.yaml up -d
```

Grafana is pinned by image digest, restricted to loopback port 18731, and has CPU/memory limits. The data server is internal-only and mounts only sanitized JSON read-only. The Infinity plugin is version-pinned. Provisioned dashboards cannot be edited through the Viewer role. The owner-protected hub must already enforce Access before installing `hub-location.conf`; never make a public bypass for this view. The Grafana admin secret is separate from Cloudflare Access authentication.

`build-dashboard.py` regenerates `dashboards/fleet-atlas.json` without credentials. The JSONata queries require a fresh snapshot. Grafana's provisioning interval is 15 seconds; container restarts use `unless-stopped`. The collector task starts at user sign-in; Docker Desktop and the hub must be running for remote reporting.

## Work scheduling

Enable **Discover useful maintenance work** in local Fleet Control, set a concurrency target, and choose an explicit review reserve count. **Use all current workers** stages the current roster count and zero reserves; **Save capacity policy** persists the choices without starting work. Protected **Startup** defaults to **Start working after verification**; uncheck it for restore-only operation.

Priority issue ownership, concrete CI fixes and independent reviews take precedence. Documentation fallback is generated from immutable Git source and actual AST findings; missing documentation is not proof of a defect. Each source blob is assigned once, each agent has at most one outstanding maintenance task, and a no-new-gap report retires the opportunity without creating filler. Outputs remain review-required drafts outside all worker Git worktrees. Protected-main integration and existing file ownership remain unchanged.

Queued start times are conditional estimates based on current capacity, pending deliveries, batching and CPU pacing. If timing depends on an unknown completion, only the next evaluation time is shown. Reserved, waiting, settling and submitted states are never counted as executing. Resource holds, approval decisions, uncertain deliveries and quota limits are not bypassed to force utilization.

## Verification

```powershell
node tools/fleet_control/check-fleet.js
node tools/fleet_control/test-browser-controls-isolated.js
python -m pytest -q -p no:cacheprovider tests/unit/test_fleet_analytics.py
python -m ruff check tools/fleet_control/collect-analytics.py tools/fleet_control/Build-Productive-Catalog.py tools/fleet_control/observability/build-dashboard.py tests/unit/test_fleet_analytics.py
```

The isolated browser test uses inert fixture routes. Live Grafana query checks must confirm nonempty frames and no query errors; browser checks must separately verify rendering, freshness, mobile layout, agent drilldowns, and the owner-protected external login. An unauthenticated Access denial is not proof that an authenticated external browser session was tested.
