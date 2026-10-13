# Mission Control — Acceptance history and time travel

The **Release acceptance** section now records a revisioned, local history of
the authoritative original MJ-REQ and supplemental MJ-REF acceptance matrices.

## What the numbers mean

- 366 original MJ-REQ and 59 supplemental MJ-REF requirements are separate.
- **PASS** requires full criterion evidence; PARTIAL and NOT_RUN do not count
  fractionally toward accepted progress.
- A commit, merged pull request, successful CI, or generated document is NOT
  automatically a PASS. The historical display never writes acceptance files.
- Every observation saves the **individual requirement results** as well as
  group counts by feature area and priority, plus release status.
- Identical observations are deduplicated to avoid wasting disk and confusing
  graphs; **Capture now** creates a labeled manual checkpoint even if no
  acceptance result changed.

## Using the UI

1. Open **Mission Control** at the stable localhost dashboard port.
2. In **Release acceptance**, use **Acceptance history & time travel**.
3. Select a captured timestamp or drag the time slider.
4. Hover the current progress bar to see the selected historical PASS/PARTIAL/
   NOT_RUN counts, current counts, and the signed change in accepted totals.
5. Expand **Original requirements**, **Additional reference gaps** and
   **Release readiness**, or any feature area or priority. Individual
   requirements show status transitions (e.g. NOT_RUN → PASS or PASS → PARTIAL).
6. Use **Expand all** / **Collapse all** to inspect details. Sections can also
   be opened and closed individually.
7. **Capture now** marks a timestamped manual observation, which is useful
   before starting an agent sprint or after reviewing a milestone.

Negative deltas are preserved and shown in red. Zero deltas do not prove no
code was written: acceptance counts move only when evidence is accepted.

## Storage and persistence

Data is saved to a local per-MJobs-checkout SQLite database:
`%LOCALAPPDATA%\MJobs\MissionControl\acceptance-<root-hash>.sqlite3`.

The hash is derived from the absolute canonical Git checkout root, so a
temporary test fixture cannot contaminate the real MJobs timeline.
The history is *not* a tracked Git file and is not rewritten during
integration or dependency installs. Make a normal filesystem backup if
moving machines.

The dashboard automatically saves a baseline on the first successful
`GET /api/progress`; later distinct acceptance states are captured
automatically. It never creates fake entries for older dates. When first
installed, the timeline has only the first genuine capture until more
acceptance states or manual captures exist.

API:
- `GET /api/progress/history?limit=120` — chronological checkpoint summary.
- `GET /api/progress/history/{id}` — original complete snapshot for drilldowns.
- `GET /api/progress/history/diff/{before}/{after}` — signed, exact-ID transitions.
- `POST /api/progress/history/capture` — explicit manual capture, protected by
  the Mission Control launch token.

The endpoints do not contact AI models or make network requests to GitHub.
The app remains bound to loopback only.

## Regression checks

```powershell
python -m pytest -q tests/unit/test_mission_progress_history.py
python -m pytest -q tests/browser/test_mission_progress_history_browser.py
node --check tools/agent_dashboard/static/progress_history_ui.js
python -m tools.ci.check_repository
```
