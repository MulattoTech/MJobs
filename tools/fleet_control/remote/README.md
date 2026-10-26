# Remote Mission Control monitor

This is a one-way, read-only publisher for the existing protected M-TechLabs service hub.
It does not proxy browser requests to either local control plane and has no start, stop,
launch, dispatch, or agent editing endpoint. Keep the existing loopback and origin guards
on Fleet Control and Mission Control.

## Layout and operation

Copy `public/index.html`, `public/app.js`, and `public/style.css` to the existing hub's
`site/mission-control/` directory. The hub serves that directory under
`https://m-techlabs.com/mission-control/` using its existing owner-only Cloudflare Access
application and tunnel JWT checks. Verify unauthenticated requests still redirect to
Access or deny before publishing a snapshot. No new public hostname or tunnel route
is needed.

From an installed Fleet Control source tree, use the supplied installer to copy the
static assets, add the hub card, preserve a backup, and register/start the hidden
normal-user monitor:

```powershell
pwsh -NoProfile -File C:\Tools\AO-Fleet-Control\Install-Fleet-Remote-View.ps1 -InstallTask -RunNow
```

The task is **MJobs Fleet - Remote Status Standard**. It uses an Interactive/Limited
principal, starts at sign-in, and has no execution time limit. A singleton mutex and
the task's `IgnoreNew` setting prevent duplicate publishers. The existing Docker hub
and Cloudflare tunnel keep their own startup and access settings.

Run with the existing Node runtime as the normal signed-in user:

```powershell
node C:\Tools\AO-Fleet-Control\remote\export-status.js --output C:\Tools\Dev\MTechLabs-Hub\site\mission-control --watch
```

Without `--watch`, the exporter writes one snapshot and exits. In watch mode it publishes
every ten seconds, with an eight-second bound on each loopback request. Use the repository's
hidden Limited scheduled task wrapper, with `MultipleInstances=IgnoreNew`, so duplicate
dashboard launches do not create duplicate monitors. The exporter itself never starts or
stops agents. It only writes `status.json` via a same-directory temporary file and rename.

The six source URLs are hardcoded GETs to ports 8787 and 18729. Raw API responses are
never persisted. A fixed field projection excludes authorization tokens, local paths,
commands, prompts, process trees, and logs. Source failures replace the affected readings
with unavailable values on the next snapshot; the exporter never copies a previous
healthy reading into a new publication. Preserved emergency rosters are labeled and
cannot produce a working-agent count. Armed quota protection, remaining allowance,
controller observations, and dispatch are separate facts.

If the master status endpoint reports its runtime inspection is still pending, the
exporter makes at most two additional GET requests, 250 milliseconds apart. All three
reads share a single eight-second deadline. This lets a settling inspection be observed
within the same publication instead of missing the server's short result cache. An
inspection that remains pending stays unverified; no earlier result is reused.

The browser fetches only `./status.json` and renders strings with `textContent`. It
refreshes every ten seconds, checks freshness every second, and removes current-state
claims after 45 seconds without a valid publication. The hub's existing `no-store`,
same-origin CSP, and Access protection apply to HTML, assets, and JSON. This page needs
both the PC and Docker service hub running for remote viewing; a fresh monitor publication
alone does not imply the fleet is running.

## Verification

```powershell
node --test remote/test-exporter.js
```

Tests cover secret-field exclusion, source failures, fixed GET-only targets, oversized and
invalid responses, stale upstream observations, preserved offline rosters, a stopped
exporter's UI state, and atomic JSON replacement. Runtime and signed-in browser checks
remain separate from these deterministic tests.
