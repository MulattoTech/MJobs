# Maintainer reference: Mission Control acceptance history

## Source identity and scope

This note describes the implementation at commit **2f01b876762fcbdd70f11e69865a48257503af93**, source path **tools/agent_dashboard/progress_history.py**, full Git blob **4186712d98c83e75cf46b2cb139aa9f5dce31f71**. It is a maintainer reference for the module’s contracts, not a new acceptance claim. No production code or acceptance data is changed by this module; its module header states observations are local and that it contacts neither GitHub nor a model (source lines 1–5).

The existing guide, docs/MISSION_CONTROL_ACCEPTANCE_HISTORY.md, already documents the user-facing panel, storage location, endpoints, and regression commands (lines 19–71). This reference adds lower-level behavior that guide does not specify: capture validation, content identity, exact method results, exception behavior, and safe direct use.

## Data flow and dependencies

The capture path starts at current_capture(root) (source lines 23–72). It converts root to Path, calls tools.verification.progress_dashboard.snapshot(root) to obtain validated catalogue totals/readiness, then reads planning/CAPABILITY_MATRIX.json, planning/REFERENCE_GAP_MATRIX.json, and planning/ISSUE_SEED.jsonl. Original requirements are keyed by requirement ID and enriched with the matching seed’s domain, priority, and issue number. Reference rows retain their ID and status. The returned mapping includes source label, each matrix’s as-of value, release and milestone summaries, PASS/PARTIAL/NOT_RUN totals, area and priority totals, and full item maps. The function checks unknown requirement IDs, reference ID-set mismatch, unexpected catalogue counts, and statuses outside ALLOWED; it raises ValueError for those explicit validation failures (lines 33–51). File, JSON, and other validation exceptions are not swallowed here.

The module uses Python standard-library collections, datetime, hashlib, json, os, pathlib, sqlite3, threading, and typing (lines 8–16). Its own capture/persistence path reads local files and uses local SQLite; it contains no GitHub or model client. The surrounding FastAPI app constructs the history object and passes it to DeliveryLedger (server.py lines 67–69). The delivery ledger later reads stored observation rows as captured transitions (delivery_ledger.py lines 223–250, 538–570); other delivery-ledger collectors are separate and should not be conflated with this module’s no-network guarantee.

## Public callable contracts

### current_capture(root)

Input: repository root path. Output: canonical matrix-derived capture described above. It validates catalog identity/count/status but does not decide whether evidence is truly acceptable beyond the matrices and snapshot validator; it records existing statuses rather than editing them (lines 23–72).

### state_fingerprint(data)

Input: a capture mapping with requirements, references, release_readiness, and milestone. Output: lowercase SHA-256 hex digest of compact, key-sorted JSON (lines 75–83). Its canonical payload contains only each requirement/reference status plus release_readiness and milestone. It intentionally excludes capture/as-of timestamps and also excludes item metadata such as area, priority, and issue. Therefore observe() deduplicates based on that narrower identity; metadata-only changes do not by themselves create a new observation.

### compare(current, previous)

Inputs: two capture mappings, current first and previous second. Output: signed original/reference PASS deltas; signed PASS/PARTIAL/NOT_RUN/total deltas by area and priority; status transitions for requirements and references; and changed_count (lines 86–119). Newly present and removed IDs use ABSENT as the missing-side status. Transitions are status changes, not proof that a feature was implemented or accepted; see the function docstring and the existing acceptance guide.

### ProgressHistory(root, db_path=None)

The constructor resolves root, derives a 16-hex-character per-root key from the lowercased resolved path, and defaults the database name to acceptance-<root-key>.sqlite3 beside DEFAULT_DB. DEFAULT_DB uses LOCALAPPDATA when set, otherwise ~/.local/share, under MJobs/MissionControl (lines 18–20, 122–140). Supplying db_path overrides that filename/location. Construction creates the parent directory, the observations table, and a captured_at/id index. Rows contain autoincrement ID, UTC capture time, fingerprint, serialized capture, and a reason constrained to changed/manual/baseline (lines 128–140). SQLite connections use an eight-second timeout (line 140); a per-instance RLock wraps the public database operations (lines 127, 150, 164, 180). The module contains inserts and reads, not update/delete or pruning logic. MAX_POINTS limits a timeline query; it is not a database-retention cap (lines 20, 162–177).

### observe(force=False, reason="changed")

Captures current matrices, fingerprints the result, then compares it with the newest stored row under the instance lock (lines 147–160). If the fingerprint is unchanged and force is false, it creates no row and returns created=false with the existing snapshot ID/time and fingerprint. Otherwise it inserts and returns created=true, new ID/time, fingerprint, and reason. Persisted reason is baseline for the first row, changed for a later changed capture, or manual when force=true. The reason argument is present in the signature but the implementation does not read its value; reason selection is derived from force and whether a previous row exists (lines 147, 151–160). A forced observation creates a manual checkpoint even when content is unchanged.

### timeline(limit=120)

Accepts limits 1 through MAX_POINTS (500); otherwise raises ValueError (lines 162–164). It selects the newest rows, reverses them into chronological order, and returns compact point summaries, total_returned, first_capture (the oldest stored capture time, even when not in the returned slice), and a note that only observed history is shown (lines 164–177). It does not return each item’s full map; use detail for that.

### detail(snapshot_id) and difference(older_id, newer_id)

Detail returns the stored ID, timestamp, reason, and full serialized capture; missing IDs raise KeyError (lines 179–184). Difference loads both IDs through detail, returns before/after ID/time labels, and calls compare(newer, older) so signed transitions run from the older capture to the newer capture; missing IDs therefore also raise KeyError (lines 185–191).

## Application entry points and error mapping

The app creates ProgressHistory in tools/agent_dashboard/server.py and exposes GET /api/progress/history, GET /api/progress/history/{snapshot_id}, GET /api/progress/history/diff/{before_id}/{after_id}, and POST /api/progress/history/capture (server.py lines 154–188). GET /api/progress also attempts an automatic capture when mission progress is available (lines 154–162). The explicit POST calls mission.require_token before capture; direct use of this route therefore requires the app’s launch token. The existing guide documents these routes and the loopback boundary (docs/MISSION_CONTROL_ACCEPTANCE_HISTORY.md lines 54–62).

The history routes map a missing detail/diff ID to HTTP 404, reject an invalid query limit through FastAPI query validation, and map ValueError/OSError/KeyError during capture to HTTP 503 for the manual route (server.py lines 164–188; tests/unit/test_mission_progress_history.py lines 77–99). Automatic GET capture catches ValueError/OSError/KeyError and embeds a generic history_capture error (server.py lines 154–162). The direct class does not translate SQLite, JSON, or filesystem errors beyond its own explicit checks; callers should not assume every failure becomes a domain-specific result.

## Safe maintainer examples

To inspect behavior without touching the normal history database, give the constructor a temporary SQLite path. This reads the selected checkout’s acceptance inputs and writes only the temporary observation database; it does not modify the planning matrices or call GitHub/models:

    from pathlib import Path
    from tempfile import TemporaryDirectory
    from tools.agent_dashboard.progress_history import ProgressHistory

    repo = Path.cwd()  # run from the intended checkout root
    with TemporaryDirectory(prefix="mjobs-progress-history-") as scratch:
        history = ProgressHistory(repo, db_path=Path(scratch) / "history.sqlite3")
        capture = history.observe()
        points = history.timeline(limit=20)
        if capture["created"]:
            full_snapshot = history.detail(capture["snapshot_id"])

For an existing local database, timeline/detail/difference are the read-oriented calls. Difference takes older ID first and newer ID second:

    delta = history.difference(older_id, newer_id)["delta"]

Do not use force=True unless a deliberately labeled manual observation is wanted. Do not infer a feature completion from a PASS-count delta; acceptance remains governed by the authoritative matrices and their evidence rules.

## Regression references and limits

The unit tests cover canonical capture totals, append/dedupe/manual capture, forward and backward signed transitions, root-specific DB isolation, source-matrix immutability, missing IDs/invalid limits, and route-token behavior (tests/unit/test_mission_progress_history.py lines 17–99). The browser test covers the dashboard interaction path (tests/browser/test_mission_progress_history_browser.py lines 17–71). The existing regression command list is docs/MISSION_CONTROL_ACCEPTANCE_HISTORY.md lines 64–71.

This draft was produced by static source/test/documentation inspection only. No tests, CI, browser runs, acceptance re-adjudication, or runtime behavior are claimed or performed.
