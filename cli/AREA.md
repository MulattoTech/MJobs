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
