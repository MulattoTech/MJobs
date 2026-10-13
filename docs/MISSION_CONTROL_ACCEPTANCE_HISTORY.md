# MJobs Mission Control acceptance history

The live Release acceptance panel remains grounded in CAPABILITY_MATRIX.json (366 original requirements) and REFERENCE_GAP_MATRIX.json (59 supplemental gaps). Git commits and passing CI are NOT acceptance evidence.

## How to use

Open http://127.0.0.1:18727/ after deployment, then locate Release acceptance -> Acceptance over time.
Choose a Trend metric (overall, reference gaps, a feature area, or P0-P6 priority). Hover over a timeline dot to read that capture's timestamp and accepted/total count; click the dot, use the slider, or use the baseline selector to compare against today.
Hover over an acceptance bar to see the earlier count, current count, delta in accepted criteria and denominator changes. Expand or collapse any summary, area, priority, and individual reference gap. Expand all / Collapse all works across the entire tracker.
After an agent run, click Save checkpoint now. It stores an explicit canonical observation even if zero requirements changed, so unchanged runs are visible as flat timeline points. Automatic captures occur only when canonical acceptance statuses change.

## Source and accuracy

Local SQLite file: %LOCALAPPDATA%\MJobs\MissionControl\acceptance-history.sqlite3
All canonical captures store MJ-REQ and MJ-REF statuses, accepted/partial/not-run counts, area and priority totals, observation timestamps, and provenance. The database is outside Git. The status tracker does not edit requirements, start agents, execute tests, or submit applications.
Screenshot imports are explicitly identified as user_screenshot. They contain aggregate bars only; no per-ID status transitions can be inferred. Two canonical captures can show exactly which requirement IDs changed status. A negative acceptance delta is displayed, not hidden. A denominator change is reported separately.
An inconsistent or concurrently edited matrix may prevent a capture; the live dashboard still displays available canonical data.

## Existing screenshot baseline

On MulattoTechBox the screenshot transcription is stored at:
%LOCALAPPDATA%\MJobs\MissionControl\screenshot_baseline_20261012.json
Original accepted 106/366; supplemental accepted 19/59; all fifteen area totals and seven priority totals are included.
The timestamp 2026-10-12 3:49:31 PM Central came from the screenshot UI's latest-local-check display; the true screenshot capture time is not independently known.
After the reviewed version is merged and deployed, import idempotently with the following PowerShell command from the MJobs Mission Control managed checkout:
python -m tools.agent_dashboard.acceptance_history --root C:\Tools\Dev\MJobs --screenshot-json "$env:LOCALAPPDATA\MJobs\MissionControl\screenshot_baseline_20261012.json"

## Verification

python -m pytest -q tests/unit/test_acceptance_history.py tests/browser/test_mission_acceptance_history.py
node --check tools/agent_dashboard/static/acceptance_timeline.js
python -m tools.ci.check_repository

Only fully accepted requirements are counted PASS. A PR merge cannot make an unverified requirement pass. Record exact-run checkpoints when you want to see whether an agent run actually increased acceptance.
