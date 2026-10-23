# Database guide

Scope: **MJ-REQ-286 / [issue #287](https://github.com/MulattoTech/MJobs/issues/287)**.
This guide describes the source contract at `8ad5d3841cb7f67ce0e204c7e169bd9ebf3e0ece`;
it is not evidence that a particular running database is current or recoverable.
The relational map below is the ER-diagram equivalent and remains readable without
a diagram renderer. Recheck source and migration heads when upgrading.

## Storage and service boundaries

| Store | Authority and consumers | Recovery boundary |
| --- | --- | --- |
| PostgreSQL 17, `database` volume | Durable profiles, jobs, applications, documents' metadata, audit and worker outbox; API, scheduler and workers share it | Back up together with document bytes; migrations are a separate privileged operation |
| Redis 7, `queue` volume | RQ delivery and worker registration; Compose enables append-only persistence | Not the application-history authority; local backup excludes it and restore creates a fresh queue |
| `documents` volume at `/data/documents` | Generated files and uploaded resumes; database rows retain references, versions and checksums | A database dump alone is not a complete document backup |
| `runtime-database` volume | Provisioned runtime connection credential, mounted read-only in runtime services | Secret, not a diagnostic artifact; provision after schema validation |
| `migration-backups` volume | Offline migration authorization material shared with the migration process | Not a substitute for the independently retained full private archive |

Source: [Compose topology](../compose.yml), [Store](../packages/persistence/store.py),
[outbox dispatch](../workers/dispatch.py), [backup implementation](../cli/local_backup.py).
PostgreSQL and Redis have no published host ports in the default topology. The
private network is internal; services with explicit egress have additional networks.
Separate worker queues do not imply separate databases or failure domains.

`Store.migrate()` is **SQLite-only test setup**, not the deployment migration path.
SQLite enables foreign-key enforcement but cannot certify PostgreSQL privileges,
advisory locking or connected recovery. Deployment uses Alembic and guarded
provisioning; do not replace it with `metadata.create_all()`.

## Relational map (ER equivalent)

Authority: [SQLAlchemy schema](../packages/persistence/models.py) and
[resume workspace schema](../packages/persistence/resume_workspace.py), applied
through [versioned migrations](../migrations/versions). In this selected core map,
each row means `child.column -> parent.id`. Parents may have zero or many children;
the final column states whether a child may omit its parent. It is not an exhaustive
column catalogue. The helper-created tables use a string `id` primary key.

| Child column | Referenced parent | Nullable FK? |
| --- | --- | --- |
| `profile_versions.profile_id` | `career_profiles` | No |
| `career_facts.profile_id` | `career_profiles` | No |
| `uploaded_resumes.profile_id` | `career_profiles` | No |
| `resume_revisions.upload_id` | `uploaded_resumes` | No |
| `companies.parent_company_id` | `companies` | Yes |
| `company_aliases.company_id` | `companies` | No |
| `company_domains.company_id` | `companies` | No |
| `company_ats_tenants.company_id` | `companies` | No |
| `company_job_sources.company_id` | `companies` | No |
| `company_job_sources.ats_tenant_id` | `company_ats_tenants` | Yes |
| `company_job_sources.collector_source_id` | `job_sources` | Yes |
| `canonical_jobs.company_id` | `companies` | No |
| `job_postings.job_id` | `canonical_jobs` | No |
| `posting_snapshots.posting_id` | `job_postings` | No |
| `application_routes.job_id` | `canonical_jobs` | No |
| `application_routes.company_id` | `companies` | No |
| `applications.job_id` | `canonical_jobs` | No |
| `applications.route_id` | `application_routes` | No |
| `applications.profile_id` | `career_profiles` | No |
| `documents.application_id` | `applications` | No |
| `application_attempts.application_id` | `applications` | No |
| `provider_credentials.profile_id` | `career_profiles` | No |
| `resume_workspace_revisions.profile_id` | `career_profiles` | No |
| `resume_workspace_revisions.job_id` | `canonical_jobs` | No |

Important constraints and non-edges:

- `applications` is unique on `(job_id, profile_id)`; multiple attempt rows are
  history for an application, not permission to submit it again.
- `job_postings` is unique on `(source, source_job_id)`. A canonical job can retain
  several source postings and each posting can retain retrieval snapshots.
- `application_routes` is unique on `(company_id, provider, tenant, requisition)`.
  Provider identity and company identity are not interchangeable with posting URLs.
- `worker_operations.entity_id` is a **logical**, kind-dependent reference, not an
  FK. `params` and audit `entity_id` also must not be drawn as enforced FK edges.
- `applications.profile_version`, `documents.profile_version`, and fact-ID JSON
  preserve provenance but are not relational FKs to `profile_versions` or facts.
  Preserve the source/version checks in application code, not just referential integrity.
- `workspace_identity` binds PERSONAL or FIXTURE mode. Do not edit that row to
  convert an existing workspace; Store rejects incompatible or unreviewed legacy
  state. Use the established workspace review/adoption flow.
- Delete behavior is per constraint, not universal cascading. For example,
  provider credentials and resume workspace revisions declare profile cascades;
  application history must not be assumed to cascade the same way.

The wider schema also contains discovery requests, monitoring and quality evidence,
AI settings/evaluation runs, resume layout revisions and recovery history snapshots.
Use the schema modules and migration chain for exact fields and checks; this map
does not authorize dropping omitted tables.

## Credentials and least privilege

[Provisioning](../cli/provision_database.py) uses the administrator connection only
in the offline migration/provision service. It creates `mjobs_runtime` with login
but without superuser, database/role creation, replication or bypass-RLS authority.
Runtime receives application-table SELECT/INSERT/UPDATE/DELETE, sequence
USAGE/SELECT and read-only access to `alembic_version`, not migration DDL rights.
Unexpected role ownership, memberships or effective privileges cause refusal;
operators must investigate rather than broadening runtime grants to make startup pass.

[Runtime credential loading](../cli/runtime_database.py) reads
`MJOBS_DATABASE_URL_FILE` in Compose and rejects missing, malformed, symlinked or
ambiguous credentials rather than falling back to the administrator. The runtime
URL is derived from protected administrator configuration for the database/role;
do not print the file, environment or connection URL. Failed provisioning must not
overwrite the last usable credential. The API and AI worker additionally receive
the provider-credential encryption key; database credentials are not that key.

## Schema lifecycle and safe upgrades

1. Use a reviewed checkout/image with a single known Alembic head. The applied
   revision is in `alembic_version`; the model file alone does not prove deployment
   state. [Alembic environment](../migrations/env.py) defines migration execution.
2. For a populated database with a pending upgrade, obtain exclusive operator
   control and follow [guarded offline migration](USER_TESTING.md#guarded-offline-database-migration).
   Keep PostgreSQL running, stop all writers (and fixture mock), and choose a new
   archive under a private current-user-owned directory outside Git.
3. `python -m cli.guarded_migration --project <actual-project> --archive <new-private-archive>`
   creates and verifies the backup, then supplies fresh one-use target-bound
   authorization internally. This is a **mutating operator action**, not a health probe.
4. [Migration preconditions](../cli/migration_precondition.py) check source/target
   revision, database/workspace identity and schema signature. Catalog-proven empty
   bootstrap and already-current validation are distinct from a populated upgrade.
5. If validation fails, preserve the archive, source database and stopped writers.
   Diagnose before restarting. Do not stamp Alembic, reuse receipts, manually change
   its version row, downgrade blindly or recreate volumes to bypass the guard.

New schema changes require separate migration ownership and PostgreSQL testing;
this guide changes neither schema nor migration behavior.

## Backup and recovery

Follow the authoritative [local backup/fresh-project restore runbook](runbooks/LOCAL_BACKUP.md).
Its [implementation](../cli/local_backup.py) pairs a PostgreSQL dump with document
bytes, mode/revision metadata and checksums while all writers are stopped. Fixture
archives additionally preserve independent mock receipts. Archive verification is
not proof that a restore or connected user flow has succeeded.

- Keep archives private and outside Git. They contain applicant data. Protected
  `.env` configuration, the original provider encryption key and browser profiles
  are **not** copied into the archive. Retain the key separately; a newly generated
  key cannot decrypt existing provider credentials.
- Restore only to a different, unused Compose project with no existing containers
  or named volumes, using the matching Compose source hash. The target PostgreSQL
  password may differ; the provider encryption key must still decrypt saved secrets.
- Restore validates revision/mode, credentials and document inventory before
  accepting readiness. Preserve partial targets after failure; do not delete an
  occupied project or use `down -v` as a retry shortcut.
- Redis delivery is rebuilt from durable operations. That does **not** authorize
  retrying an uncertain application submission. Keep `SUBMISSION_UNCONFIRMED`
  blocked pending evidence/reconciliation; never manufacture provider receipts.
- Recheck profile/job/application readback and checksum-verified downloads in the
  fresh target. Runtime readiness alone is not full business-flow acceptance.

## Safe diagnosis (read-only)

Start with the established API `/readiness` response and
`docker compose -p <actual-project> ps`; inspect only sanitized service errors.
Never paste `docker inspect`, rendered secret-bearing configuration, raw database
rows, `.env`, dumps or runtime credential contents into an issue or PR.

An authorized operator already connected through a protected PostgreSQL client can
inspect schema metadata with the following transaction. Do not place a password or
connection URL in shell history. These queries neither repair nor migrate anything:

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '3s';
SET LOCAL lock_timeout = '1s';
SELECT version_num FROM alembic_version;
SELECT operation_mode FROM workspace_identity WHERE id = 'workspace';
SELECT table_name FROM information_schema.tables
 WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
 ORDER BY table_name;
COMMIT;
```

If a query fails, `ROLLBACK` the transaction; do not switch to administrator rights
as an automatic workaround. Missing schema, denied runtime privileges, dependency
outage and stale worker heartbeat need different diagnoses. See
[runtime health](../services/runtime_health.py); a Redis failure is not evidence
that PostgreSQL history is lost.

## Verification boundaries

Relevant local regression commands (run from the repository root with cached dependencies):

```powershell
python -m pytest -q tests/unit/test_database_provisioning.py tests/unit/test_runtime_database.py
python -m tools.verification.catalogue
git diff --check
```

The unit checks exercise credential rejection and failure-preservation behavior;
they do not prove live PostgreSQL grants or restore success. Record their actual
results against the PR head. Connected migration, backup/restore, browser readback
and hosted checks remain **NOT_RUN** until separately executed on that exact revision
with an allocated isolated runtime. Never run those against a personal database
merely to validate this document. Real application submission remains OFF.
