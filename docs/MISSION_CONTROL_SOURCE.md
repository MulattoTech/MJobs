# Mission Control source and deployment

Mission Control source is maintained in MulattoTech/MJobs under
tools/agent_dashboard/, with Start-MJobs-MissionControl.ps1 and
Launch-MJobs-MissionControl.vbs as entry points.

The automatic Fleet controller and its tested source installer live in
MulattoTech/Fleet-Control-Hub, package packages/mjobs-controller/.
Do not copy that controller implementation back into MJobs.

This change versions the previously installed Mission Control runtime additions:
windowless background Git/provider probes, graceful unavailable GPU telemetry,
and live AO session observations separate from independent launcher records.
The observation panel routes mode and capacity choices to the single Fleet
Control selector. It can pause new assignments; current turns continue.
It does not start a second dispatcher or change quota settings.

Private runtime state, credentials, conversation history and machine logs are
external to both source repositories. Installed Mission Control snapshots are
verified by the source-manifest-aware launcher in Fleet-Control-Hub.

## Reuse boundary

Mission Control currently depends on MJobs planning and acceptance modules.
Its code is versioned, but installing it into an arbitrary project still requires
extracting those dependencies into project adapters. Fleet-Control-Hub owns that
future shared packaging work. A project preference alone is not an installation.

## Validation

Run the focused background console regressions and affected existing dashboard
tests. Check JavaScript syntax and verify the live observation panel reports
fresh runtime state and links to Fleet Control without dispatching work on load.
