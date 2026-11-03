# AI-assisted resume wording with saved fact references

MJobs can explicitly request resume wording from its configured
resume_tailoring provider, save a private proposal, and prepare a resume only
after the user reviews every proposed sentence. The resulting DOCX, PDF and TXT
retain their immutable profile version, source fact IDs, original wording/source
and the AI proposal identity. The application's document history displays the
AI provider/model separately from the deterministic rendering method.

This is a command-line drafting workflow. Draft controls are not yet exposed in
the web UI. The existing web history and download views display accepted results.
Cover letters prepared alongside the resume continue using the existing
deterministic approved-fact workflow.

## Configuration and boundaries

Use an existing active workspace and the same protected document root as MJobs.
Supply MJOBS_DATABASE_URL through protected environment configuration. The
command never migrates a database or enables application submissions.

Choose a supported provider/model for the profile's **Resume tailoring** task in
AI settings. Disabled is the default. This command uses that provider's protected
runtime configuration, such as MJOBS_LOCAL_AI_BASE_URL for a local endpoint.
It refuses a saved profile credential for the selected provider: it does not
silently substitute environment credentials or redirect a saved key. Support for
saved-profile keys is a separate follow-up.

Only an explicit draft command with consent contacts the provider. The context,
review and prepare commands make no model calls. Selected approved resume facts
and bounded target-job title/description are sent; profile names, contact fields,
answers, source metadata and unselected facts are excluded. Selected facts can
themselves contain personal information, so review them before consenting.
No endpoint or credential is accepted from model output.

## 1. Inspect the current source context

Use the Python environment that runs MJobs. The examples use placeholder IDs;
replace them with the displayed values. Keep command output private.

~~~powershell
python -m cli.ai_resume context --profile-id PROFILE_ID --job-id JOB_ID --output-dir DOCUMENT_ROOT
~~~

This prints eligible approved facts and their IDs, the current profile version,
the target-job fingerprint and the selected provider/model. No provider is called.

## 2. Request one proposal

Use a new request ID of 16–128 letters, digits, underscores or hyphens for each
deliberately new attempt. Select at most 20 supported resume facts. Add multiple
--fact-id arguments when needed.

~~~powershell
python -m cli.ai_resume draft --profile-id PROFILE_ID --job-id JOB_ID --output-dir DOCUMENT_ROOT --fact-id FACT_ID --request-id resume-request-0001 --expected-profile-version PROFILE_VERSION --expected-job-fingerprint JOB_FINGERPRINT --consent-send-selected-facts
~~~

The provider receives a closed JSON schema. A successful result returns an opaque
draft ID, SHA-256 and REVIEW_REQUIRED. It creates no application or document.
A request is limited to 1–60 seconds (30 by default), a 32 KiB prompt and the
existing 100,000-character structured response cap. Each proposed sentence is
limited to 4,000 characters; saved draft bytes are capped at 128 KiB.

The request receipt is committed before provider invocation. Repeating a
completed identical request returns the existing draft. A failed, interrupted or
uncertain request is **never automatically sent again**. Investigate its status
and explicitly choose a new request ID if another provider call is wanted.
Provider timeout/cancellation and malformed replies publish no accepted document.

## 3. Review the exact proposal

~~~powershell
python -m cli.ai_resume review --profile-id PROFILE_ID --output-dir DOCUMENT_ROOT --draft-id DRAFT_ID
~~~

The private output shows each proposed sentence beside its original immutable
source, plus the ordered selected fact IDs and review SHA-256. Dates, numbers
and named source tokens are checked, but those checks cannot establish semantic
equivalence. Confirm that every meaning and claim is supported by the source.

Proposals with changed protected tokens, missing/duplicate/unselected facts or
unexpected response fields are refused. The provider cannot set review flags.
A stale profile or target job requires a new proposal. The current workflow does
not edit a sealed AI draft in place.

## 4. Prepare the reviewed document

Copy the exact SHA-256 and fact IDs in the order returned by review:

~~~powershell
python -m cli.ai_resume prepare --profile-id PROFILE_ID --output-dir DOCUMENT_ROOT --draft-id DRAFT_ID --reviewed-sha256 REVIEW_SHA256 --reviewed-fact-id FACT_ID --confirm-reviewed
~~~

Preparation revalidates the immutable inputs under the existing profile/job
transaction locks. It produces the existing checksum-validated DOCX/PDF/TXT
formats and returns the actual saved application state and document version.
Repeating an identical acceptance preserves the revision and any later
application state. It does not submit an employer application.

Open **Applications → document history → Generation details and facts** to see
the original facts, source labels, reviewed wording and AI attribution.
A later profile/job edit does not rewrite historical fact references.

## Storage, recovery and provenance

Drafts live in the protected document root's .ai-resume-drafts directory.
Include this directory with the existing database/document backup pair.
REQUESTED, READY and FAILED audit receipts contain only hashes and fixed metadata,
not prompts, proposal text or credentials. A receipt seals each draft's bytes;
changing the file cannot forge accepted attribution.

Accepted documents preserve enough proposal content and original fact references
to validate their attribution against the READY receipt even after the private
draft file has been archived. Original source snapshots and audit history remain
required. Corrupt attribution blocks the validated history preview.

The deterministic renderer's implementation manifest still truthfully records
that the renderer did not execute AI. Separate ai_proposal metadata records the
wording provider, model, versioned prompt contract, source references, result hash
and explicit user-review policy. Neither metadata layer claims that a model
verified the applicant's qualifications.

## Verification

Focused suites:

~~~text
python -m pytest -q tests/integration/test_ai_resume_fact_trace.py
python -m pytest -q tests/integration/test_ai_resume_fact_trace_postgres.py
python -m pytest -q tests/browser/test_ai_resume_fact_trace.py
~~~

PostgreSQL tests require the existing explicitly disposable loopback
MJOBS_AI_EVALUATION_POSTGRES_URL fixture and use a unique temporary schema.
The loopback-provider regression exercises the real local HTTP provider and CLI
against a synthetic server. No paid inference or external application submission
is needed to verify this contract.
