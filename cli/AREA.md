# Local command ownership

The `cli` package owns project-scoped startup probes, safe fixture verification,
workspace identity inspection, and offline local backup/restore commands. It
does not own the API's durable state or authorization decisions. The backup
interface is `python -m cli.local_backup {backup,verify,restore}`; restore only
creates a fresh Compose project and never overwrites existing volumes.

## Read-only maintenance entry point (MJ-REQ-207, partial)

`python -m cli health` reads the existing `/health` liveness contract.
`python -m cli source list` reads the existing `/sources` contract and prints
only source ID, name, provider, enabled flag, and health status. Override the
default API port with `--base-url http://127.0.0.1:4321` **before** the command.

Only literal numeric HTTP loopback origins (`127.0.0.1` or `[::1]`) are
allowed. The CLI never follows redirects or proxies, sends writes, executes
source adapters, submits applications, or prints API source URLs, credentials,
or arbitrary response fields. Request and response budgets are bounded and
errors print stable codes rather than private server payloads. This is a useful
initial, read-only slice of issue #208, not acceptance of the full maintenance
CLI requirement; other conceptual actions remain unimplemented. Deterministic
regressions are in `tests/unit/test_cli_main.py`.

`python -m cli.init_env` only creates a missing project `.env`. It stages the
private secret in the same directory, flushes it, and publishes with atomic
create-if-absent semantics; concurrent or repeated initialization preserves
the existing configuration. Importing this module has no filesystem effect.
Synthetic failure, concurrency, private-file and stdout-redaction tests live in
`tests/unit/test_init_env.py`.

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
