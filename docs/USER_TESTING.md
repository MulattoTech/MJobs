# Local user-testing guide

## Start

Install/start Docker Desktop in Linux-container mode. In PowerShell:

```powershell
cd C:\Tools\Dev\MJobs
.\Start-MJobs.ps1
```

Open http://127.0.0.1:18700. First startup downloads/builds the images. The script
creates an ignored private `.env` atomically only when absent; existing
configuration and volumes are preserved. It waits for PostgreSQL, Redis, the
scheduler, all three worker queues, and the mock service. No separate
Node/Python install is needed
for normal use.
If an older `.env` lacks `MJOBS_CREDENTIAL_ENCRYPTION_KEY`, the launcher preserves
it and stops before Compose. Follow the [configuration recovery runbook](runbooks/WORKSPACE_MODE.md);
never replace a key that protected stored credentials.
For startup without opening a browser, add `-NoOpen`. Use `-NoBuild` only when
the current source has already been built.

For personal job-search operation, run `./Start-MJobs.ps1 -Personal`. The
launcher defaults to the separate `mjobs-personal` Compose project and port
18701, so its PostgreSQL, queue, documents and mock receipts do not share the
fictional verification project's volumes. Personal mode hides the demo and
rejects fictional discovery, preparation and mock submission at the API and
worker, including old queued fixture work. Public Greenhouse reads, matching,
factual document preparation and manual application links remain available.
Switching modes does not copy profiles or documents between projects. External
auto-submit remains disabled; this milestone is not a release-readiness claim.
The API and workers bind each fresh database to one operation mode and refuse a
mode flip. An older database with data but no binding is preserved and pauses
startup for explicit review; see the [workspace mode runbook](runbooks/WORKSPACE_MODE.md).

## Try the full safe journey

1. Choose **Load fictional demo**. This creates a separate labeled profile and
   two fictional jobs; it does not replace your own profile.
2. Search for **Data**, select **Data Engineer**, and inspect the skill coverage,
   missing skills, source postings, retrieval time, and snapshot checksums.
3. In **Profile**, remove approval from SQL and save. Its fact becomes pending;
   matching and generated documents exclude it. Restore approval and save to
   include it again. Verification and approval are separate explicit controls.
4. Select the job and choose **Prepare documents**. The background worker creates
   resume and cover-letter PDF/DOCX downloads from eligible facts. Profile edits
   do not change existing files. Preparing again after an edit creates a new
   revision while retaining the old revision. Confirmed or active submissions
   cannot have their history replaced.
5. **Run dry-run** fills and validates the local two-page mock form, uploads the
   prepared files, and stops before submission. Inspect `DRY_RUN_COMPLETE` and
   `STOP_BEFORE_SUBMIT` in the tracking evidence.
6. To test fictional confirmation, check **I approve one fictional mock
   submission**, then choose **Submit to local mock**. The local receipt has a
   submission count of one. Repeated requests reuse the durable operation.
7. Reload the page or restart the fictional workspace with `docker compose --profile fixture restart`. The selected profile,
   document revisions, application history, and receipt remain available.

## Use your own profile and real public listings

Choose **New profile** and enter your contact details and career facts. Use a
single skill name for a skill fact (for example, `Python`); other fact types can
contain factual experience statements. Supply evidence/source, confidence,
verification, and approval. Facts below confidence 80, unsourced facts, and
pending facts are excluded. No experience, qualification, or answer is inferred.

After saving and selecting the profile, use **Base resume files** in **Profile**
to store a PDF, DOCX, or UTF-8 TXT original up to 10 MiB. The local copy is
marked **STORED UNREVIEWED** and can be downloaded for a checksum-checked
readback. It does not extract, verify, approve, or use facts automatically;
enter and review career facts separately. Offline backup includes the uploaded
bytes and metadata together with the rest of the document volume and database.

In **Sources**, enter an employer's Greenhouse board token, such as the token
in its Greenhouse careers URL. Each source exposes its declared capabilities and
keeps independent poll interval, timeout, rate-limit, concurrency, jitter,
health, cooldown and provider-backoff policy. **Collect now** runs an immediate
bounded collection with a selected career profile.

To run discovery continuously, create one or more **Scheduled search profiles**
for the selected career profile. Give each search a name and desired titles,
remote preference, employment type, and either one source or all enabled
sources. The local scheduler evaluates each source independently, queues only
durable work when that source is due, and never receives external-network
access itself; the discovery worker remains the egress boundary. Pause/resume
is persistent. The Greenhouse board was verified live again on 2026-10-01.
Public listings can change.

Collection performs bounded public GET requests, preserves native posting IDs
and raw snapshots, and records health/backoff. Repeated clicks and scheduler
ticks cannot override cooldown, active-operation deduplication, or provider
backoff. After profile edits, **Refresh matches** recalculates stored jobs
without contacting the provider.

Live matching measures coverage of detected skill mentions. It does not prove
mandatory requirements or overall suitability. Missing salary/remote data stays
unknown. Distinct native posting IDs remain separate even when titles match.

Real jobs support factual document preparation and an **Open employer
application** link. Their tracker entry is `PREPARED`, labeled **Manual only**.
Submit through the employer's own site yourself if you choose. This milestone
has no real submission implementation or enablement control; server-side gates
also reject attempts to run the mock adapter on real applications.

## Diagnostics and recovery

`docker compose ps` reports health for every long-running service. The API uses
`/ready`, so a running process is unhealthy when its database, Redis, scheduler,
required workers, or fixture mock is unavailable. Each worker checks its own
container registration, queue, live process and fresh heartbeat; the scheduler
checks its last successful tick. The one-shot migration service has no ongoing
health check. Allow startup and heartbeat grace periods before diagnosing an
unhealthy service, then inspect that service's bounded logs. Health probes do not
restart containers or delete data.

**Activity** shows database/queue/worker readiness and, in fictional verification
mode, mock readiness. Durable operation
states. Failed, blocked, and cancelled work stops UI polling and displays an
error category. Cancellation is available before a submission reservation;
an uncertain or reserved submission requires reconciliation instead of retry.
An interrupted browser before submission becomes `REVIEW_REQUIRED`.

If Docker is stopped, start it and run the startup command again. Inspect
`docker compose logs --tail 80 api source-worker document-worker worker` when
an operation fails. `SOURCE_COOLDOWN` and `SOURCE_BACKOFF` mean wait for the
recorded policy time; `DOCUMENT_NO_APPROVED_FACTS` means approve and verify at
least one sourced fact. `DOCUMENT_PROFILE_CHANGED` means prepare again from
the current profile. Do not delete volumes to retry; they contain local data.
For an offline PostgreSQL/document backup, including uploaded base resumes,
and a tested fresh-project restore,
follow the [local backup runbook](runbooks/LOCAL_BACKUP.md). It preserves the
original project and refuses an occupied restore target.

## Guarded offline database migration

For a populated database with a pending reviewed Alembic upgrade, use the matching
reviewed checkout and migrate image. Keep PostgreSQL running; explicitly stop the
API, scheduler, every worker and migrate service first (also mock in fixture mode),
as described in the [backup runbook](runbooks/LOCAL_BACKUP.md). Retain exclusive
operator control: do not start writers until validation succeeds.

Choose a **new** archive beneath an existing private, current-user-owned directory
outside this checkout; the command refuses unsafe permissions or an occupied path.
From the repository root, using the actual project name, run:

```powershell
.venv\Scripts\python.exe -m cli.guarded_migration --project <project> --archive <new-private-archive>
```

The command creates and verifies a full backup with PostgreSQL17 native tools,
then internally passes a fresh target-bound, one-use receipt to migrate. Do not
copy/reuse receipts or supply secrets on the command line. Required schema and
workspace identity are checked before runtime credentials are published. A failed
command preserves the archive and leaves writers stopped for inspection; it never
automatically restores, deletes or recreates the source database. Keep the backup
and private configuration; recovery is a separate reviewed operator action. Fresh
catalog-proven empty bootstrap and validated already-current provisioning remain
supported; a populated pending upgrade cannot bypass this guarded path.

## Executable verification

Development checks use the pinned Python environment and installed Playwright
Chromium. On this managed Windows session, SQLite tests require writable temp
paths under the project:

```powershell
$env:TEMP = Join-Path (Get-Location) 'outputs\test-tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
.venv\Scripts\python.exe -m pytest -q tests/unit tests/integration
.venv\Scripts\python.exe -m tools.verification.catalogue
.venv\Scripts\python.exe -m cli.ui_e2e --evidence outputs/ui-proof.json
# Recovery intentionally requires a separately launched mjobs-fixture-<12 hex>
# project and its explicit port; never target personal data.
```

The Compose image build runs frontend tests, strict TypeScript checking, and a
production Vite build under pinned Node 24. Host development checks also use
`npm --prefix apps/web test -- --run` and `npm --prefix apps/web run build`. Live source
verification is opt-in with `python -m cli.public_source_probe --tenant <token>
--evidence outputs/public-source.json`; it respects the persisted cooldown.
Browser readback can select that exact source/profile with
`python -m cli.ui_source_readback --baseline outputs/public-source.json --evidence outputs/public-browser.json`.

## Limits of this milestone

- Local single-user testing only; the API is bound to loopback and has no
  multi-user authentication or authorization. Personal data remains in ignored
  local database/document/browser volumes. Do not publish those volumes.
- Configured Greenhouse boards support live public read-only collection. Remotive,
  RemoteOK and We Work Remotely adapters also exist; each provider needs separate
  live verification. Company discovery, source enrollment, deduplication and
  recurring scheduling have implementation and controlled tests; their complete
  connected live-source acceptance remains open.
- Deterministic tailoring preserves approved facts, records provenance and supports
  explicit fact selection, contact headers, comparisons and reviewed adjustments.
  Resume import and additional document workflows exist. Full document-quality and
  AI-provider acceptance remains open; no paid AI call is required for the tested
  deterministic journey.
- Real applications support manual preparation and user-reported tracking. Live
  Greenhouse/Lever browser filling and submission are not verified. External
  submission stays OFF; mock confirmation proves only the controlled fixture.
- A normal generation failure/cancellation removes its uncommitted files. A
  process kill can leave unreferenced files in the local document volume; no
  automatic retention/garbage-collection policy is implemented yet.
- Public GitHub Actions run reviewed CI and synthetic checks independently. A missing,
  failing, or incomplete hosted check is not a PASS; local validation alone does not
  establish full release or catalogue acceptance. Paid AI reviewers remain disabled.

The full requirement catalogue is preserved. These limits are delivery scope,
not deleted requirements.

## Companies/Sources controls and activity retest
Once the normal local runtime is healthy, use the existing VS Code live-feedback task and open **Sources**. Learned company sources and manually configured public boards are separate sections. An empty learned-source section is not evidence that there are no jobs. Source counts reflect explicitly linked postings; Relevant Jobs counts current-version profile match records, not eligibility.

For an existing learned source, toggle Pin/Unpin, Disable/Enable and Watch/Unwatch, then reload and inspect the saved state. Review **Recent discovery** for recorded enrollment/import messages and **View Evidence** for the bounded underlying metadata. Missing history is not inferred. Run Now, Rediscover, Verify, Edit and next-check scheduling remain unavailable in this view.

The regression test `python -m unittest tests.browser.test_company_source_view -v` runs this flow with synthetic data after `npm run build --prefix apps/web`; it does not use a personal browser profile or submit applications. Private `outputs/live-feedback/**` files must remain outside Git history. The workstation Docker Linux engine was unavailable during the October 2 verification; hosted Compose success does not establish local runtime readiness.
