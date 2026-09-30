# Local user-testing guide

## Start

Install/start Docker Desktop in Linux-container mode. In PowerShell:

```powershell
cd C:\Tools\Dev\MJobs
.\Start-MJobs.ps1
```

Open http://127.0.0.1:18700. First startup downloads/builds the images. The script
creates an ignored private `.env` only when absent; existing configuration and
volumes are preserved. It waits for PostgreSQL, Redis, all three worker queues,
and the mock service. No separate Node/Python install is needed for normal use.
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
The production API refuses personal startup against a database containing mock
application routes. Use a separate project instead of deleting verification data.

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

In **Sources**, enter an employer's Greenhouse board token, such as the token
in its Greenhouse careers URL, and choose **Collect jobs** with a profile active.
The `greenhouse` board was verified live on 2026-09-30. Public listings can change.
Collection performs bounded public GET requests, preserves native posting IDs
and raw snapshots, and records health/backoff. Default collection cooldown is
five minutes. Repeated clicks cannot override cooldown or provider backoff.
After profile edits, **Refresh matches** recalculates stored jobs without
contacting the provider.

Live matching measures coverage of detected skill mentions. It does not prove
mandatory requirements or overall suitability. Missing salary/remote data stays
unknown. Distinct native posting IDs remain separate even when titles match.

Real jobs support factual document preparation and an **Open employer
application** link. Their tracker entry is `PREPARED`, labeled **Manual only**.
Submit through the employer's own site yourself if you choose. This milestone
has no real submission implementation or enablement control; server-side gates
also reject attempts to run the mock adapter on real applications.

## Diagnostics and recovery

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

## Executable verification

Development checks use the pinned Python environment and installed Playwright
Chromium. On this managed Windows session, SQLite tests require writable temp
paths under the project:

```powershell
$env:TEMP = Join-Path (Get-Location) 'outputs\test-tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.venv\Scripts\python.exe -m tools.verification.catalogue
.venv\Scripts\python.exe -m cli.ui_e2e --evidence outputs/ui-proof.json
.venv\Scripts\python.exe -m cli.compose_recovery --baseline outputs/ui-proof.json --evidence outputs/recovery-proof.json
```

The Compose image build runs frontend tests, strict TypeScript checking, and a
production Vite build under Node 24. This avoids the host's unsupported Node 25
test-runner version and managed Windows child-process restriction. Live source
verification is opt-in with `python -m cli.public_source_probe --tenant <token>
--evidence outputs/public-source.json`; it respects the persisted cooldown.

## Limits of this milestone

- Local single-user testing only; the API is bound to loopback and has no
  multi-user authentication or authorization. Personal data remains in ignored
  local database/document/browser volumes. Do not publish those volumes.
- Greenhouse configured public boards are the only live provider implemented.
  No global aggregator search, automatic employer-site enrollment, recurring
  scheduling, or real application automation is claimed.
- Documents are factual fact-list templates, not a completed tailoring/AI system.
  Imports, advanced matching, portfolio layout, and broad document-quality gates
  remain in the backlog.
- Real applications have manual preparation tracking; updating manually submitted
  statuses and manual CAPTCHA/login intervention resume remain future work.
- A normal generation failure/cancellation removes its uncommitted files. A
  process kill can leave unreferenced files in the local document volume; no
  automatic retention/garbage-collection policy is implemented yet.
- GitHub Actions publication/execution requires the account token's `workflow`
  scope. The reviewed workflow is staged in `infra/ci/verification.yml`.
  Local validation is separate from remote CI; implementation PRs remain drafts
  while this gate is unresolved.

The full requirement catalogue is preserved. These limits are delivery scope,
not deleted requirements.
