# Local command ownership

The `cli` package owns project-scoped startup probes, safe fixture verification,
workspace identity inspection, and offline local backup/restore commands. It
does not own the API's durable state or authorization decisions. The backup
interface is `python -m cli.local_backup {backup,verify,restore}`; restore only
creates a fresh Compose project and never overwrites existing volumes.

Requirements touched: MJ-REQ-013, MJ-REQ-014, MJ-REQ-167, MJ-REQ-168. Related
areas: `packages/persistence`, `services/documents`, and `workers`. Tests live
in `tests/unit/test_local_backup.py`; the isolated Compose recovery evidence is
under `docs/evidence`. Run targeted tests with
`.venv\Scripts\python.exe -m unittest tests.unit.test_local_backup -v`.

Do not print database secrets or personal archive contents, back up live writers,
copy browser cookies into a portable archive, bypass bound workspace mode, or
automatically retry an uncertain application submission. New storage surfaces
must join the backup/restore proof before being described as protected.

The standalone development-feedback page is `python -m cli.feedback_gui`,
launched by `Start-Feedback.cmd` / `Start-Feedback.ps1`. Issue #451 adds guided
task forms and private SQLite drafts beneath `outputs/live-feedback`, with
browser recovery storage, revision conflicts, and atomic local handoff files.
It is related to MJ-REQ-184, but does not complete runtime error management.
Static assets live in `cli/feedback_ui`; `cli/feedback_tasks.json` owns the
19-task catalogue. Tests are `tests/unit/test_feedback_gui.py`,
`tests/unit/test_feedback_tasks.py`, and `tests/browser/test_feedback_gui.py`.
The server binds only to loopback and never executes submitted content, launches
agents, publishes reports, or changes application submission policy. Feedback
storage is separate from the product database and its backup coverage.
