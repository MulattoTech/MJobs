# Workspace operation mode

Each MJobs database binds to `FIXTURE` or `PERSONAL` on its first empty startup. The
API and workers refuse the opposite mode before reading profiles or claiming work.
An older database with data and no binding is preserved and refuses startup until
its owner reviews and explicitly binds it. Migrations add only the identity table;
they do not rewrite profiles, jobs, applications, documents or operations.

## Inspect an older database

Stop the project's API and workers, retain a backup of its named volumes, and
apply the new schema without starting the API. For the default fictional
project, run:

```powershell
docker compose -p mjobs --profile fixture stop api worker source-worker document-worker
docker compose -p mjobs up -d postgres
docker compose -p mjobs --profile fixture build migrate api
docker compose -p mjobs --profile fixture run --rm --no-deps migrate
docker compose -p mjobs --profile fixture run --rm --no-deps api python -m cli.workspace_identity inspect
```

For a personal project, replace `mjobs` with `mjobs-personal` and omit
`--profile fixture` in each command. The inspect command prints table counts and a SHA-256 fingerprint of
the current database rows. It does not print profile content or credentials.
Check the project and its data history before selecting a mode. Rows alone cannot
reliably distinguish a fixture profile from a real one.

## Bind reviewed legacy data

Only after confirming the project's intended mode, run the same one-off container
with an explicit mode and the exact fingerprint just inspected:

```powershell
docker compose -p mjobs --profile fixture run --rm --no-deps api python -m cli.workspace_identity adopt --mode FIXTURE --expect-fingerprint <64-hex-fingerprint> --acknowledge-reviewed-data
```

The adoption transaction rechecks every row and refuses changed data. It creates
one identity record without editing business rows. Use `--mode PERSONAL` only for
a verified personal database. Then start the project with `Start-MJobs.ps1` or
`Start-MJobs.ps1 -Personal` as appropriate. A later mode flip is refused.

If the project cannot be confidently classified, keep it stopped and preserve its
volumes for review. Do not copy its rows into another mode or remove the identity
record to bypass the check. A code rollback may retain the new identity table; a
migration downgrade refuses to drop it after binding.
