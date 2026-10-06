# Generated document artifact recovery

Document revisions are generated in a private staging directory, validated, and
published to an immutable revision path. An interrupted write cannot replace a
previous revision. Answer exports use the same-directory atomic-write pattern.
No background cleanup runs automatically.

Recovery commands require an explicit local artifact directory and a protected
`MJOBS_DATABASE_URL` bound to an active MJobs workspace. The inventory reports
references and classifications without reading artifact contents into output.
Uploads, referenced revisions, debug captures, symlinks, and unknown files are
protected. Only recognized generated orphans older than 24 hours are eligible.

```powershell
$env:MJOBS_DATABASE_URL = '<protected local database URL>'
$artifacts = 'C:\path\to\private\MJobs\documents'
.\.venv\Scripts\python.exe -m cli.document_artifacts scan --output-dir $artifacts
```

Quarantine requires copying an eligible `artifact_id` from the scan. It moves
the artifact into `.quarantine` and records an audit event. Restore requires the
quarantine ID and refuses to overwrite an existing destination. Purge keeps
quarantined bytes for at least one day (30 days by default), verifies the
recorded digest before removal, and leaves a `PURGED` manifest and audit record.

```powershell
.\.venv\Scripts\python.exe -m cli.document_artifacts quarantine --output-dir $artifacts --artifact-id '<artifact-id>'
.\.venv\Scripts\python.exe -m cli.document_artifacts list --output-dir $artifacts
.\.venv\Scripts\python.exe -m cli.document_artifacts restore --output-dir $artifacts --quarantine-id '<quarantine-id>'
.\.venv\Scripts\python.exe -m cli.document_artifacts purge --output-dir $artifacts
```

These commands are explicit local maintenance actions. They do not submit
applications, contact job sites, or start an automatic retention worker.
