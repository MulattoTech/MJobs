# Local backup and fresh-project restore

This procedure covers the PostgreSQL database and local document volume for one
Compose project. It has been exercised with isolated FIXTURE and PERSONAL
projects. The archive contains career profiles, application history, source
catalog data, generated documents and uploaded base resumes. Treat it as sensitive personal data and
store it outside this Git checkout on a protected local disk.

The archive does **not** copy `.env` secrets, Redis's rebuildable queue, or
Playwright browser profiles and cookies. Keep private host configuration through
your normal protected local backup process. In particular, provider credentials
remain encrypted in the database and require the original
`MJOBS_CREDENTIAL_ENCRYPTION_KEY` from protected secret storage. A newly
generated key cannot decrypt them. Keep the key outside the archive and restore
it into the private project `.env` before running the restore. The database
password may be newly generated for the fresh target. The PostgreSQL outbox
requeues unfinished work when workers start. A restored browser flow may require
fresh authentication and operator review; `SUBMISSION_UNCONFIRMED` must never be
blindly resubmitted.

## Back up a quiescent project

Run from the repository root with Docker Desktop and the project's Python
environment available. This example is for the default personal project; use
its actual Compose project name if different. Choose a new archive path outside
the repository. Existing archives are never overwritten.

```powershell
$project = 'mjobs-personal'
docker compose -p $project stop api worker source-worker document-worker scheduler company-discovery-worker
.venv\Scripts\python.exe -m cli.local_backup backup --project $project --archive 'D:\PrivateBackups\mjobs-2026-10-01'
.venv\Scripts\python.exe -m cli.local_backup verify --archive 'D:\PrivateBackups\mjobs-2026-10-01'
```

The backup command refuses to run while the API, scheduler, any worker, or a migration
container is active. PostgreSQL must remain running. It writes a custom-format
logical dump, copies the document volume, excludes browser profiles, records
the bound FIXTURE/PERSONAL mode and migration revision, and checks SHA-256 for
every included file. It rechecks that writers remain stopped during capture
and publishes the archive only after verification. Keep exclusive operator
control of this Compose project until the command finishes; do not restart a
writer or modify its document volume from another process during capture.

New PERSONAL archives use manifest schema 2; FIXTURE archives use schema 3.
Both include the public `compose.yml` source
plus `configuration/runtime.json`. The runtime snapshot records the API
container's operation and mock modes, bounded concurrency values, and transient
retention interval. It excludes `.env`, database/Redis URLs, encryption keys,
passwords, arbitrary retention text, and unknown environment variables. The
operation and mock modes are archival context only; the archive never supplies
settings to Compose during restore. Review the snapshot manually after restore
if those non-secret settings matter to the operator. Schema 1 archives remain
verifiable and restorable without a configuration snapshot.

For FIXTURE projects, also stop `mock` before backup. Schema 3 captures and
checksums only `mock/receipts.json`, the fictional provider's independent
confirmation history. It verifies every confirmed MOCK application's receipt
is present. A new fixture with no confirmed applications may have an empty
receipt file. A missing, corrupt or incomplete receipt history fails without
publishing an archive; preserve the source project for diagnosis. PERSONAL
archives never inspect, include or restore fixture receipts.

After the backup, restart the original project with its established launcher
and port. For the default personal project:

```powershell
.\Start-MJobs.ps1 -Personal -NoBuild -NoOpen
```

## Restore into a different project

Use the matching repository version and Compose configuration. The archive's
Compose SHA-256 must match the checkout. Before restoring, retrieve the original
`MJOBS_CREDENTIAL_ENCRYPTION_KEY` from the protected local secret store used for
the source project. The database dump contains encrypted provider credentials;
the archive never contains their key. If `.env` is missing, `cli.init_env`
creates a new private file with a new key, so replace that generated key with
the original value using the protected local configuration process before
restoring. Keep the key out of command output, shell history, and the archive.
The PostgreSQL password in the fresh target's `.env` may be new.

Build the application image from the matching checkout before restoring.

```powershell
.venv\Scripts\python.exe -m cli.init_env
docker compose build api
.venv\Scripts\python.exe -m cli.local_backup verify --archive 'D:\PrivateBackups\mjobs-2026-10-01'
.venv\Scripts\python.exe -m cli.local_backup restore --archive 'D:\PrivateBackups\mjobs-2026-10-01' --target-project mjobs-recovered --port 18712
Invoke-RestMethod http://127.0.0.1:18712/readiness
```

The target name must differ from the source and have no existing Compose
containers or named volumes. Restore refuses an occupied target and never
deletes its data. It starts a fresh PostgreSQL volume, restores the database in
one transaction, checks migration revision and operation mode, then verifies
that the configured key decrypts every saved provider credential before it
starts the restored application. A wrong or missing key fails closed; the new
target may contain the restored database and must be preserved for diagnosis.
With the original key installed, restore copies and checks the exact restored
document inventory and hashes, starts the correct mode, and requires readiness
with external submission disabled. Redis starts with a fresh volume; durable
pending operations are recovered from PostgreSQL.

FIXTURE schema-3 restore copies the independent receipts into the fresh mock
volume before starting services. Legacy schema-1/2 FIXTURE archives remain
verifiable, but a restore with confirmed mock applications stops after database
restore because those archives omitted the independent receipts. Keep that
partial target and the source volumes for diagnosis; do not reconstruct a
provider receipt from a database confirmation or replay an uncertain submission.
Capture a new schema-3 backup from the intact source when available. Legacy
fixtures without confirmed applications and all PERSONAL archives retain their
existing restore path.

Restore verifies and retains `configuration/runtime.json` for manual review;
it never applies archived operation/mock flags or replaces the current Compose
environment. A configuration snapshot does not enable external submission or
change the existing restore service startup policy.

If restore fails after creating the target, leave its partial volumes intact
for diagnosis. Fix the cause and use another new target name. Do not use
`docker compose down -v` on an existing project as a retry shortcut. Review
the restored profiles, source catalog, applications and document downloads
before treating the project as a replacement for the original. This procedure
does not move data back into an occupied project.

If credential verification fails, retrieve and install the original key in the
private `.env` through the protected secret-management process, then restore to
a different new target name. The failure output does not include the key or
credential values.

## Validation scope

The isolated proof in `docs/evidence/2026-09-30-pr004-backup-restore-local.json`
restored a fictional confirmed application, profile, source catalog, four
document downloads and a queued operation into a different FIXTURE project.
A separate PERSONAL restore preserved a manually prepared real-job tracker
entry, first-party public posting provenance, profile, source catalog and four
document downloads with external submission disabled. A later isolated PERSONAL
browser run uploaded a synthetic base resume, and a fresh-project restore
preserved its metadata and exact bytes while keeping every CareerFact unapproved
(`docs/evidence/2026-09-30-pr004-upload-restore-local.json`). A pre-upload
archive also restored into a new project and upgraded to the new schema without
losing its profile or 21 public jobs. Neither proof submitted a real external
application. Protected host configuration, browser authentication and physical
host restart remain outside this validation.
