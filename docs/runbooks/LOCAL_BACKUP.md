# Local backup and fresh-project restore

This procedure covers the PostgreSQL database and local document volume for one
Compose project. It has been exercised with isolated FIXTURE and PERSONAL
projects. The archive contains career profiles, application history, source
catalog data, generated documents and uploaded base resumes. Treat it as sensitive personal data and
store it outside this Git checkout on a protected local disk.

The database/document archive does **not** copy `.env` secrets, Redis's rebuildable queue, or
Playwright browser profiles and cookies. Use the separate encrypted configuration
companion below for explicitly selected `.env` and private Compose files, or keep
them through your established protected backup process. In particular, provider credentials
remain encrypted in the database and require the original
`MJOBS_CREDENTIAL_ENCRYPTION_KEY` from protected secret storage. A newly
generated key cannot decrypt them. Keep the key outside the archive and restore
it into the private project `.env` before running the restore. The database
password may be newly generated for the fresh target. The PostgreSQL outbox
requeues unfinished work when workers start. A restored browser flow may require
fresh authentication and operator review; `SUBMISSION_UNCONFIRMED` must never be
blindly resubmitted.

## Back up and recover protected configuration

`cli.protected_config_backup` captures exact bytes from explicitly selected files.
It does not inspect environment variables, discover files recursively, interpret
dotenv values, execute Compose, or change an active project. Keep the ordinary
database/document archive and this configuration companion together in your
recovery inventory; their independent recovery key belongs in a separate
protected location. Stop editing the selected files while capturing them. The
tool rejects a file that changes during capture.

Prepare existing private directories for the recovery key, encrypted archive,
and recovered files. On POSIX, these directories must be owned by the current
user with no group/other access; selected source files must also be private
(normally mode `0600`). On Windows the tool verifies NTFS ownership and DACLs
for parents and each secret file. Only the current user, SYSTEM, and local
Administrators may be granted access to a protected object; ancestors must not
allow other principals to replace it. A directory or file that cannot be
verified is refused. The tool does not modify existing permissions. A Python
mode argument or `chmod` alone is not proof of private NTFS access.

The example locations below must already satisfy these checks. The key location
must be separate from the archive directory and the source/recovery directories.
Keep a separately protected recovery copy of that key outside the computer whose
failure the backup must survive. Never place it alongside the encrypted archive
or include it in the selected configuration files.

```powershell
.venv\Scripts\python.exe -m cli.protected_config_backup keygen --key-file 'E:\MJobsRecoveryKeys\config.key'
.venv\Scripts\python.exe -m cli.protected_config_backup backup --source-root 'C:\Tools\Dev\MJobs' --file '.env' --file 'compose.private.yml' --key-file 'E:\MJobsRecoveryKeys\config.key' --archive 'D:\PrivateBackups\mjobs-config-2026-11-02.mjpc'
.venv\Scripts\python.exe -m cli.protected_config_backup verify --key-file 'E:\MJobsRecoveryKeys\config.key' --archive 'D:\PrivateBackups\mjobs-config-2026-11-02.mjpc'
.venv\Scripts\python.exe -m cli.protected_config_backup restore --key-file 'E:\MJobsRecoveryKeys\config.key' --archive 'D:\PrivateBackups\mjobs-config-2026-11-02.mjpc' --destination 'D:\PrivateRecovery\mjobs-config-2026-11-02'
```

Select only files that exist and matter to that project; omit the second `--file`
when there is no private Compose override. Accepted names are `.env` and flat
`compose.<name>.yml` or `compose.<name>.yaml` files, where `<name>` starts with a
lowercase letter/digit and contains up to 64 lowercase letters, digits,
underscores, or hyphens. Public `compose.yml` remains in the ordinary archive.
At most eight files, 2 MiB per file, and 8 MiB in total are accepted. Symlinks,
junctions/reparse points, hardlinks, special files, subdirectories, and traversal
names are refused.

The versioned companion encrypts filenames and contents using AES-256-GCM with
a fresh random nonce. The separate binary recovery key is 32 random bytes and
is independent of `MJOBS_CREDENTIAL_ENCRYPTION_KEY`, which can be one of the
values protected inside `.env`. The key is never printed or included in the
archive. Losing the recovery key makes the companion unrecoverable. A new
recovery key cannot decrypt an existing archive.

`verify` authenticates the complete archive without writing plaintext. `restore`
authenticates and validates all files before creating a fresh private directory.
Wrong keys, corruption, unsafe members, and oversized data fail before that
directory is created. Existing key files, archives, restore directories, and
concurrently created output files are never overwritten. A write failure removes
only objects still identifiable as created by this invocation; an ambiguous or
occupied partial directory is left protected for operator inspection. Process
termination can also leave private staging files. No active configuration is
replaced, and no recovered content is executed.

Successful publication requires removal of the staging link and verification
that the final private output has exactly one link. A staging-cleanup failure
reports failure, even if protected files must remain for operator inspection.
Restore retains the original identity of each file it publishes; a concurrent
replacement is never adopted as owned rollback material.

Review the recovered files privately. Before the database restore below, install
the recovered original `MJOBS_CREDENTIAL_ENCRYPTION_KEY` through your protected
configuration process, and review any private Compose override separately. The
existing database restore checks that this key decrypts saved provider
credentials before application startup. Do not put recovered values in command
output, shell arguments/history, issue comments, or public test evidence.
This companion does not capture browser cookies or remove the need for fresh
authentication and operator review.

## Back up a quiescent project

Run from the repository root with Docker Desktop and the project's Python
environment available. This example is for the default personal project; use
its actual Compose project name if different. Choose a new archive path outside
the repository. Existing archives are never overwritten.

```powershell
$project = 'mjobs-personal'
docker compose -p $project stop api worker source-worker document-worker ai-worker migrate scheduler company-discovery-worker
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

The runtime snapshot has its own version. Runtime version 2 also records the
hourly, daily, and company submission limits, including a zero cap. Readers
continue to accept the original version 1 snapshots. Version 2 requires all
three caps and validates the same decimal bounds used by admission; incomplete,
invalid, or unknown settings are rejected. These values are retained for manual
review and are never applied automatically during restore.

Runtime version 3 additionally captures the validated browser-profile default,
in-app notification enablement, and worker log level. It is emitted when the
container supplies `MJOBS_BROWSER_PROFILE`, and then requires all three settings
and the complete version-2 policy. Older containers without the selector retain
their existing snapshot version, so a pre-upgrade backup remains compatible.
All three runtime versions are read-only recovery references; restore does not
automatically apply them.

Application `browser-profile-selection.json` manifests contain only the pinned
label and schema version and are retained with the application documents. The
entire `browser-profile` directory, including every named child profile and its
cookies, is excluded. A fresh restored application recreates its pinned label
without restoring authentication. Reauthenticate explicitly before resuming any
flow requiring a session.

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
`MJOBS_CREDENTIAL_ENCRYPTION_KEY` from the protected local secret store or the
separately recovered encrypted configuration companion used for the source
project. The database dump contains encrypted provider credentials; the ordinary
database/document archive never contains their key. If `.env` is missing, `cli.init_env`
creates a new private file with a new key, so replace that generated key with
the original value using the protected local configuration process before
restoring. Keep the key out of command output, shell history, and the ordinary
database/document archive.
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

### Guarded Windows updater command capture

The existing Windows updater invokes Git, Docker and backup commands through
one bounded command reader. It drains stdout and stderr concurrently, retaining
at most 8 MiB combined. Each command has a finite deadline (120 seconds by
default, with existing explicit build/backup/Compose limits). Invalid, infinite
or excessive timeout values are rejected before process creation.

Timeout, cancellation, pipe failure and output overflow fail closed with fixed
diagnostic codes. The reader closes its pipes and makes bounded termination and
kill attempts against only the direct process it created. It does not search
for or terminate processes by executable name. A descendant holding an inherited
pipe cannot extend the capture deadline after the direct child exits.

Successful bounded output remains complete; overflow is a failure rather than
truncated success. Raw command output is never added to failure exceptions.
Backup failures retain only the existing allowlisted reason code. A failed
backup or verification leaves the candidate unactivated and uses the existing
previous-release recovery path. This improves future diagnostics; it cannot
recover the discarded cause of the historical November 1 exit-code-2 failure.

### Historical isolated restore evidence

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
host restart remain outside these historical validation runs; the encrypted
configuration companion requires its own executed recovery evidence.
