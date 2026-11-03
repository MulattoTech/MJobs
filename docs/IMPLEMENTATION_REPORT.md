# MJobs implementation report

**Verified developer-preview checkpoint; the product remains PARTIAL.**

This report fulfills the reporting scope of **MJ-REQ-295**. It does not accept
the whole-product requirements or turn a registered component into a verified
live integration. Technical inventory revision: **1350f398386d0be8c9555aa8ec565daf186b2b84**; tree:
**4c42f9ec80abc2987d91ed7308c5ef90da4dc312**. Inventory generated **2026-11-03T04:03:03.922637+00:00**. The sprint comparison starts at
`63b843dfe2706e6a5ac1d567af7e5acb01e134a7`. Test and deployed observations below
name their own revisions and times.

The authoritative acceptance records remain
[the original 366-requirement matrix](../planning/CAPABILITY_MATRIX.json) and
[the separate 59-item reference matrix](../planning/REFERENCE_GAP_MATRIX.json).
This report preserves their wider PARTIAL/NOT_RUN findings. Its exact inventory
and scoped changes are recorded in
[inventory metadata](evidence/2026-11-03-webpro-642e4b-implementation-report/inventory.json).
Historical evidence is identified as historical; no live collector or paid AI
request was performed for this report.

**Subsequent observations, 2026-11-03:** the original technical inventory and
manifest remain immutable. The Documents section separately records PR #896,
merged as `7e86511f409fb9d1e2dc79904b170cc74b88cd3d`, and Deployment records the
verified `ea5fbf9f` rollout. These dated additions update the report without
relabelling an earlier test or inventory revision. Other concurrent changes and
canonical acceptance remain governed by their own protected receipts.

## Architecture

MJobs is a self-hosted workspace for discovering jobs, matching them against
reviewed career evidence, preparing factual documents, and retaining application
history. FastAPI serves API routes and the built React/TypeScript UI. Domain
services separate discovery, company identity, canonical job identity, matching,
documents, and application workflows.

PostgreSQL owns durable business state. Redis/RQ transports work, while operation
IDs, worker claims, leases, cancellation and attempt fencing protect execution.
Queue completion and browser state are not authoritative submission receipts.
Operations are persisted before dispatch, but database/Redis dispatch is not an
exactly-once distributed transaction. Some operations also execute inline.

The model deliberately separates source postings, canonical vacancies,
application destinations, document revisions, execution attempts and confirmed
receipts. This preserves provenance and prevents one duplicated listing from
becoming permission to repeat an application. Document metadata lives in the
database and bytes in a separate volume; atomic artifact publication does not
make recovery across both stores one transaction.

Five worker categories separate discovery, company discovery, AI, documents and
applications. The default host exposure is loopback. The remote preview relies
on Cloudflare Access plus a tunnel that validates the Access audience; application
host/origin checks do not supply user authentication.

This sprint adds stricter Referer-origin fallback, duplicate security-header
rejection and affirmative same-origin admission for public-host mutations
(MJ-REQ-172), and corrects an unsupported Workday fixture-upload capability
(MJ-REQ-224). Neither changes schema, dependencies or credential configuration.

References: [architecture](ARCHITECTURE.md),
[persistence models](../packages/persistence/models.py),
[dispatch](../workers/dispatch.py), [operation execution](../workers/operations.py),
[document artifacts](../services/documents/artifacts.py),
[request boundary](security/web_request_boundary.md).
Architecture documentation acceptance under MJ-REQ-285 does not close
interruption recovery (008) or the full continuous pipeline (155).

## Files

The [complete tracked-file manifest](evidence/2026-11-03-webpro-642e4b-implementation-report/file-manifest.tsv)
contains **2,874 files** at the technical inventory revision, with Git
blob IDs and byte lengths. It is the complete implementation footprint at that
revision, not a selected list presented as exhaustive.

The complete added/modified/removed sprint delta from the stated baseline is in
`inventory.json`: **9 added, 15 modified, 0 removed**. No unknown worktree or user-data file is included.
The report, its verification receipts, discoverability link and canonical
accounting are added after this technical checkpoint and appear in the
containing PR's complete Files changed view.

The main implementation areas are `apps/api` and `apps/web`; persistence and
schema in `packages/persistence` and `migrations`; source/company discovery,
dedupe, matching, AI, documents and applications in `services`; worker execution
in `workers`; verification in `tests` and `tools/verification`; and startup,
containers and guarded release tooling in `Start-MJobs.ps1`, `compose.yml` and
`infra`. The sprint's behavioral changes are specifically in
`apps/api/main.py` and `services/applications/adapters/workday.py`, accompanied
by focused regression tests and preserved execution evidence.

## Services

The runtime/container/readiness observations in this table were read-only at
**2026-11-03T03:46:45Z**, on deployed revision
**63b843dfe2706e6a5ac1d567af7e5acb01e134a7**. It predates the sprint's changes. The separate authenticated Brave UI observation
was recorded in the earlier web-security receipt at 03:33:06Z, rather than in the
03:46:45Z runtime probe. Health is not proof of every user workflow.

| Component | Implementation and observed status |
|---|---|
| Frontend | React production assets are built with Node 24 and served by the API image; no separate frontend container. The existing authenticated Brave session displayed the ready application, with real employer submissions OFF. |
| Backend | Python 3.12 FastAPI/Uvicorn. The live API container was healthy and loopback-bound at port 18801. |
| Database | PostgreSQL 17, no published database port. Database and schema readiness were true. A migration/provisioning service precedes application startup. |
| Redis | Redis 7 with AOF persistence, no published Redis port. Queue readiness was true. |
| Scheduler | `services.discovery.scheduler`; healthy with scheduler readiness true. |
| Workers | Application, discovery, company discovery, AI and document worker roles all reported ready. All seven application services, including API/scheduler, were healthy. No paid provider call was made. |
| Browser automation | Playwright Chromium is installed in the non-root application image. Controlled browser tests passed as listed below; image inclusion and service health do not prove live ATS automation. |
| Fictional mock | `fixtures.mock_server` is enabled by the fixture profile, internal port 8090. It is separate from the PERSONAL live stack and public ATS support. |

The internal private network limits application/document workers and scheduler.
Source/company/AI workers and the API have the separately configured egress
network where required. The live micro override uses its existing dedicated
subnet; this report does not change networking.

References: [Compose](../compose.yml), [Docker image](../infra/docker/Dockerfile),
[deployment runbook](../infra/deploy/README.md).

## Sources

There are **five built-in collectors in the default registry**; configured extensions
and disabled entries can change a runtime registry. Each implements search, fetch,
normalization, request-health reporting and declared capabilities. They expose
read-only discovery/detail and application redirects, not direct applications
or application-status lookup. Four declare optional salary support; WWR does
not. A capability does not guarantee that every listing supplies that field.

| Source | Strategy and implemented working scope | Evidence and limitations |
|---|---|---|
| Greenhouse | API on `boards-api.greenhouse.io`, scoped to a configured employer board; board inventory, individual details, stable tenant/job IDs and normalized URLs. | Controlled integration and company-source contracts exist. Historical canonical notes record bounded live read-only collection/preparation; no fresh live request here. A board collector does not discover every employer globally. |
| Lever | Regional public API, with exact `global:site` or `eu:site` identity. Inventory uses 100-item pages, at most 10 pages and a 120-second inventory budget. | Controlled tests cover regional hosts, pagination, duplicates, page/budget limits and hostile identifiers. Full final pages and duplicate identities fail instead of silently publishing incomplete inventory. No live Lever acceptance established by this review. Posting time is unavailable and remains null. |
| Remotive | Public JSON at `https://remotive.com/api/remote-jobs`; multi-company normalization and redirects. | Controlled tests cover search/fetch, persistence, errors, URL boundaries, bounds and cancellation. Historical October acceptance is controlled evidence; no explicit fresh live result is claimed. |
| We Work Remotely | Fixed public RSS feed; multi-company normalization and client-side filtering. Fetch resolves feed identity; no salary capability. | Controlled contracts cover malformed feeds, redirect refusal, filtering, identity conflicts and rollback. The November 1 UTC capture, retained in the November 2 evidence record, contained 100 rows / 98 identities with two conflicting GUID/link groups; isolated replay refused `SOURCE_DUPLICATE_JOB_ID`. A later observer bookkeeping error prevented that capture from verifying persisted row counts. No new live PASS or arbitrary winning record is claimed. |
| RemoteOK | Public JSON at `https://remoteok.com/api`; multi-company, salary and remote metadata normalization. | Controlled integration coverage and historical October bounded-live wording exist. This report did not repeat or independently refresh the live observation. Live tests require explicit `MJOBS_ENABLE_LIVE_TESTS=1`. |

The [source registry](../services/discovery/sources/registry.py) is the authority
for this five-item set. The
[exact-five contract](../tests/unit/test_source_adapter_contract_223.py) exercises
search and normalization through `httpx.MockTransport`, including stable IDs,
company attribution, absent optional fields and GET-only requests; it makes no
public request. [Company-source contract coverage](../tests/unit/test_company_source_contract_sustain_224.py)
includes Greenhouse/Lever as employer collectors and the other three as global
collectors. Some older area prose still says four; the inspected registry
contains five.

Historical source receipts:
[Remotive](evidence/2026-10-06-remotive-job-source-adapter.json),
[WWR](evidence/2026-10-06-we-work-remotely-job-source-adapter.json),
[RemoteOK](evidence/2026-10-06-remote-ok-job-source-adapter.json),
[retained WWR conflict](evidence/2026-11-02-wwr-upstream-conflicts.json).
MJ-REQ-017/018 remain PARTIAL; registry completeness does not accept their
broader source and strategy requirements.

## Company Discovery

| Surface | Implemented behavior and tested boundary |
|---|---|
| Domain resolution | Supplied domains and provenance-tagged candidates are validated. The default web-search provider is disabled; there is no shipped concrete search-vendor implementation in the provider module. A search result alone does not authenticate an employer. |
| Narrow automatic origin acquisition | An eligible source-bound Greenhouse custom job URL can establish an employer domain only through matching JobPosting identity/title/origin and an exact reciprocal native-tenant/requisition link, followed by reauthorization before atomic promotion. General acquisition from company names, aggregator mentions or arbitrary ATS URLs remains incomplete. |
| Career discovery | Supported native APIs take priority. Same-origin advertised RSS/Atom/JSON Feed discovery provides a bounded fallback where independently corroborated. Ambiguous ATS evidence prevents fallback. Feeds do not create native ATS tenant records. |
| ATS detectors | Twenty-two signatures produce MATCH, AMBIGUOUS or NO_MATCH with evidence. Detection does not imply job enumeration or application capability. |
| Native source enrollment | Greenhouse and region-qualified Lever are supported. Enrollment requires verified official-domain evidence or the current explicit identity-confirmation authority, tenant uniqueness, ownership and source-policy checks. Unsupported providers fail `COMPANY_ATS_ENUMERATION_UNSUPPORTED`; ambiguous tenants require review. Existing disablement and ownership are preserved. |
| Strong-match trigger | Durable trigger and priority handling exist. Without verified origin, an existing employer source, or the narrow corroborated link path, intent remains `NEEDS_ORIGIN_REVIEW` and queues no discovery. General origin discovery and running-work preemption remain incomplete. |

Detection signatures are Greenhouse, Lever, Ashby, SmartRecruiters, Workable,
Workday, Jobvite, BambooHR, Recruitee, Teamtailor, Breezy, iCIMS, Oracle Taleo,
SuccessFactors, Dayforce, ADP Recruiting, JazzHR, ApplicantPro, UKG, Paycom,
Paylocity and NeoGov. These twenty-two detectors, five collectors and eleven
application adapters are different sets.

Controlled provider evidence covers Greenhouse/Lever employer-source contracts
and the generic feed paths; the report inventory itself did not run live company
discovery. Historical Greenhouse read-only evidence is distinct from live
verification of every detector. MJ-REQ-264 remains PARTIAL.

References: [detectors](../services/ats_detection/detector.py),
[enrollment](../services/company_discovery/ats_enrollment.py),
[origin acquisition](../services/company_discovery/domain_acquisition.py),
[feed discovery](../services/company_discovery/endpoints.py),
[discovery flow](../services/company_discovery/flywheel.py),
[optional search](../services/company_discovery/search.py).

## Deduplication

Production ingestion uses conservative identity attachment. Existing native
posting identity is sticky; a new posting can attach to one unambiguous
same-company job through exact provider/tenant/requisition identity or a
specific canonical application URL. Generic landing/search URLs, conflicting
native requisitions and ambiguous historical candidates prevent unsafe
attachment. Historical jobs and source provenance remain available for review.

Company corroboration uses official IDs, domain overlap, ATS tenant or known
identity relationships; a similar company name alone is insufficient. Strong
job signals represent requisitions, canonical application URLs and shared
source/job relationships separately from text similarity.

The deterministic similarity policy uses normalized-description SHA-256
equality and character-trigram Jaccard comparison after selected boilerplate
reduction. It is lexical comparison, not embedding or AI semantic matching.

| Policy condition | Default confidence or disposition |
|---|---:|
| Corroborated company plus exact requisition or source relationship | 1.00 |
| Same company plus canonical application URL | 0.97 |
| Same company/title plus equal normalized description | 0.94 |
| Same company/title/location and description similarity at least 0.72 | 0.87 |
| Same company/title and similarity at least 0.65 | Maximum of 0.65 and rounded similarity |
| Insufficient evidence | At most 0.64 |
| Disposition thresholds | At least 0.95: AUTO_MERGE_ELIGIBLE; at least 0.65: DUPLICATE_REVIEW_REQUIRED; below: SEPARATE |

These are the policy helper's actual defaults. No production caller of
`duplicate_confidence` was found in the inspected application/service/persistence
call sites. They do not mean production ingestion automatically merges fuzzy
matches at 0.95. The declared `near_identical_description=0.90` setting is not used
by that helper. Review decisions persist MERGE, KEEP_SEPARATE and NEVER_MERGE;
recording MERGE_APPROVED does not itself consolidate canonical jobs.

Application-route dedupe separately prioritizes company/requisition, then
tenant/requisition, then canonical URL, then route ID. Equivalent routes retain
aliases and an authority-ranked representative. Planning supports default,
explicit-route, employer-site and all-distinct-routes modes. Employer authority
depends on persisted evidence, not a provider label. Database uniqueness
separately constrains posting identity, route identity, profile/canonical-job
applications and selected underlying routes.

References: [ingestion identity](../services/dedupe/ingestion_identity.py),
[signals](../services/dedupe/signals.py), [policy](../services/dedupe/policy.py),
[routes](../services/dedupe/routes.py), [review decisions](../services/dedupe/reviews.py).
Recorded MJ-REQ-040–053 acceptances retain their scoped evidence; this source
description does not expand them to unimplemented historical consolidation.

## Matching

The persisted deterministic score measures coverage of approved positive career
evidence, not hiring probability. Required skills weigh 1.0, preferred 0.5,
nice-to-have 0.25 and unclassified mentions 0.25. Negative, aspirational or
conflicting facts receive no positive credit. Missing approved evidence does not
prove a person lacks a skill. Explanations retain per-skill evidence and profile
version.

A selected saved-search scoring profile can combine that baseline with supported
experience, preference and specialized signals. Unknown contributions stay null
and leave the active denominator. Without a configured profile, the existing
skill score is retained. The separate twelve-dimension matching engine exists
but is not the default persisted-job scorer.

The queued AI job-scoring consumer performs **advisory job scoring**: bounded inputs,
category/rationale and validated exact posting quotations. It does not replace
deterministic scores or generate application documents. Controls include
disabled defaults per task, structured schemas, provider/model configuration,
credential binding, cost policies, deadlines, cancellation, freshness and
durable checkpoints. Uncertain completion is not silently retried.

The job-scoring funnel's default policy uses minimum score 75, at most 5,000 input candidates,
75 AI candidates and 20 document candidates; document selection requires review.
These limits do not imply that every job is sent to a provider. Naming an AI
evaluation run during document preparation revalidates candidate context; it
does not approve AI-written wording. A separate explicit CLI consumer added
after the inventory checkpoint requests resume wording proposals and passes
reviewed drafts into the document pipeline described below.

References: [persisted matching](../services/discovery/matching.py),
[selected scoring](../services/matching/selected_search_scoring.py),
[general engine](../services/matching/engine.py),
[AI consumer](../services/ai/consumer.py),
[durable evaluation](../services/ai/durable_evaluation.py),
[cost controls](../services/ai/cost_controls.py).
MJ-REQ-108–115/117 remain PARTIAL and 118 NOT_RUN; category display and
prioritization have separate accepted slices. No paid AI execution occurred here.

## Documents

| Area | Implemented behavior and limitation |
|---|---|
| Resume parsing | PDF, DOCX and UTF-8 TXT produce reviewable facts. Durable extraction pins owner, upload, content digest, request and extractor version. Extracted facts start unverified and unapproved. |
| OCR and dates | Optional bounded local PDF OCR fallback preserves explicit uncertainty and lower confidence. Original wording, source IDs and supported date projections are retained. Ambiguous text does not create invented tenure, exact days or employer/role associations. |
| Resume tailoring | Approved-fact ordering, explicit selection/omission, skill/responsibility emphasis, summary/bullet selection, optional contact header, page fitting and reviewed role/story selections. Broad factual rewriting remains incomplete. |
| Reviewed rewrites | Eligible supplied summary, responsibility, experience, accomplishment, project and technology-experience proposals bind to source IDs/hashes, explicit review, protected dates/numbers and named source tokens. User review is not automated proof of semantic equivalence. Manually supplied proposals remain distinct from the subsequent CLI AI-draft path, which also requires explicit factual review. |
| AI-assisted resume wording (subsequent PR #896) | Explicit CLI requests save private proposals and hash-bound audit receipts. Exact draft/fact confirmation and human review precede existing DOCX/PDF/TXT preparation. Drafting has no new web/API controls or queued proposal worker; saved-profile credentials are refused, and cover letters remain deterministic. |
| Cover letters | Deterministic job/company templates rank relevant approved facts by normalized token overlap. Styles are professional, warm, technical, direct, conversational and formal; lengths are very short, concise, normal and detailed. Missing required job context or relevant approved facts blocks generation. Supplied company context stays content-unverified. |
| Factual validation | Approved sourced facts with confidence at least 80, template assertion contracts, expected-content reconstruction, protected dates/numbers and reviewed-rewrite source binding. This supports the implemented deterministic/reviewed contracts, not arbitrary generated prose. |
| Formats and publication | DOCX, PDF and UTF-8 TXT are checked for presence, readability, nonempty content, cross-format consistency and hashes before publication. File existence alone is not document readiness. |
| History | Revisions retain profile version, fact IDs, artifact hashes, template, generation model/context and timestamp. Historical facts resolve against the original version; later profile edits do not rewrite history, and missing legacy evidence is not fabricated. Subsequent AI-resume revisions also retain proposal/provider/model metadata and source hashes validated against the saved completion receipt; deterministic renderer provenance remains separate. |

References: [extraction](../services/career_profile/extraction.py),
[durable extraction](../services/career_profile/durable_extraction.py),
[generation](../services/documents/generate.py),
[tailoring](../services/documents/tailor.py),
[cover letters](../services/documents/cover_letter.py),
[validation](../services/documents/validation.py),
[output readiness](../services/documents/failure_status.py),
[history](../services/documents/history.py).

At the original inventory checkpoint, extraction, document-format and historical
trace slices were accepted while MJ-REQ-095, 100 and 269 remained PARTIAL. The
subsequent implementation below establishes a supported AI-resume path; its
canonical acceptance and deployment are separate recorded stages.

### Subsequent integrated AI-resume path

[PR #896](https://github.com/MulattoTech/MJobs/pull/896) merged at
`7e86511f409fb9d1e2dc79904b170cc74b88cd3d` on 2026-11-03 at 04:38:29 UTC.
Its [consumer source](https://github.com/MulattoTech/MJobs/blob/7e86511f409fb9d1e2dc79904b170cc74b88cd3d/services/documents/ai_resume.py)
implements explicit CLI requests for resume wording from selected approved
facts and bounded job context, with consent. A committed request receipt
precedes the call; private drafts and hash-bound completion receipts preserve
the proposal. Uncertain calls are not automatically replayed. Exact draft-hash
and fact-selection confirmation, human factual review, and profile/job
revalidation precede existing document preparation. Accepted history retains
original facts, source hashes, proposal identity and configured provider/model
attribution separately from the deterministic renderer.

The [stored execution receipt](https://github.com/MulattoTech/MJobs/blob/7e86511f409fb9d1e2dc79904b170cc74b88cd3d/docs/evidence/2026-11-03-webpro-23e5-ai-resume/source-execution.json)
records **48 tests plus 46 subtests, zero failures/skips, and 14 frontend tests**
at source `9a5fcafa44ea3213b845db23eaa9838cedd1ac1f`. This includes four
PostgreSQL cases, two Chromium cases and a real loopback HTTP-provider/CLI
fixture. Relevant consumer and test files are unchanged at head `c2718534`;
a [separate candidate checkpoint](https://github.com/MulattoTech/MJobs/pull/896#issuecomment-6595236833)
reports focused execution there. Earlier fixture, dependency and state-readback
failures remain in that worker's receipt. This report did not rerun those tests
or perform paid inference.

[Workflow limits](https://github.com/MulattoTech/MJobs/blob/7e86511f409fb9d1e2dc79904b170cc74b88cd3d/docs/AI_RESUME_DRAFTS.md)
remain explicit: drafting is CLI-only, runtime credentials are supported,
saved-profile credentials are refused, and there is no new drafting API,
web drafting control or queued proposal worker. Cover letters remain
deterministic. Token checks do not establish semantic equivalence; provider/model
metadata records configured attribution, not independent remote-model
attestation. The [acceptance candidate](https://github.com/MulattoTech/MJobs/blob/7e86511f409fb9d1e2dc79904b170cc74b88cd3d/docs/evidence/2026-11-03-webpro-23e5-ai-resume/acceptance-candidate.json)
keeps canonical promotion and deployment separate. At the observed merge,
269's canonical status was still PARTIAL; later authoritative receipts govern
any change. Broader tailoring, AI cover letters and whole-product completion
are not established by this scope.

## Applications

The [application registry](../services/applications/provider_registry.py) contains
eleven built-in adapters, with configured extension/disablement support. The following public-action boundary applies individually to
every adapter, including those with richer metadata review:

| ATS adapter | Public autofill | Public upload | Public submit | Public confirmation |
|---|---|---|---|---|
| Ashby | Unavailable | Disabled | Disabled | Unavailable |
| BambooHR | Unavailable | Disabled | Disabled | Unavailable |
| Breezy | Unavailable | Disabled | Disabled | Unavailable |
| Generic HTML | Unavailable | Disabled | Disabled | Unavailable |
| Greenhouse | Unavailable | Disabled | Disabled | Unavailable |
| Lever | Unavailable | Disabled | Disabled | Unavailable |
| Recruitee | Unavailable | Disabled | Disabled | Unavailable |
| SmartRecruiters | Unavailable | Disabled | Disabled | Unavailable |
| Teamtailor | Unavailable | Disabled | Disabled | Unavailable |
| Workable | Unavailable | Disabled | Disabled | Unavailable |
| Workday | Unavailable | Disabled | Disabled | Unavailable |

All eleven normalize routes; normalization does not prove browser navigation.
Their implemented form/question support differs:

| Adapter | Analysis/question handling | Fixture upload and limitations |
|---|---|---|
| Ashby | Static HTML analysis; known fields can be reviewed. Unknown, sensitive, challenge and malformed cases require review. | None; no DOM analyzer or native validity method. |
| BambooHR | Route recognition; form metadata extraction is false and analysis returns unsupported review. | None; all form semantics need human review. |
| Breezy | Static and DOM analysis with positive visible/editable evidence and safe prompt metadata. Unknown/sensitive/ambiguous controls, selects and textareas require review. | None; missing interactivity evidence fails closed; no native validity method. |
| Generic HTML | Recognizes supplied bounded control metadata, without claiming HTML extraction. | None; no DOM analyzer; unknown/sensitive/ambiguous/malformed controls require review. |
| Greenhouse | Static review, shared DOM metadata and native validity reanalysis detect changed form/control state. Metadata review does not answer general questions. | Loopback fixture helper only: verified resume/cover-letter snapshots, unchanged unambiguous form, required-field validity and selected-file byte/hash readback; rejects public origins. |
| Lever | Inherited DOM analysis/validity, provider route normalization and a separate static markup policy; unknown custom/sensitive/ambiguous questions require review. | Inherited loopback helper. Dedicated controlled browser metadata tests were inspected; no direct Lever browser test of that inherited upload helper was identified. |
| Recruitee | Inherited DOM analysis/validity plus challenge checks; static policy uses shared markup review. Unknown/custom/sensitive/ambiguous/challenge fields require review. | Explicitly rejects inherited upload helper. |
| SmartRecruiters | DOM metadata with exact provider-origin and challenge checks, including DataDome. Static dispatcher returns DOM_INSPECTION_REQUIRED. | None; no native validity method; unknown/sensitive/challenge fields require review. |
| Teamtailor | DOM visibility, editability, paint evidence and challenge checks. Static dispatcher requires DOM inspection. | None; no native validity method; hidden/noninteractive or ambiguous controls remain review-only. |
| Workable | DOM disabled/readonly/inert, clipping/offscreen, occlusion and challenge evidence. Static dispatcher requires DOM inspection. | None; no native validity method; contact fields without demonstrated interactivity remain review-only. |
| Workday | Static Workday-specific and bounded dynamic/repeated-field review retain question/option fingerprints and structural validation. Durable static reports are always review-only. DOM analysis/validation return WORKDAY_DOM_INSPECTION_UNAVAILABLE. | The sprint correction sets mock_fixture_upload=false and raises WORKDAY_FIXTURE_UPLOAD_UNAVAILABLE before inspecting browser/analysis/document inputs. Cancellation/deadline callback runs first and preserves exceptions. |

Adapter code is in [services/applications/adapters](../services/applications/adapters);
[static routing](../services/applications/form_review_providers.py) distinguishes
the supported analysis paths. The
[registry contract](../tests/unit/test_application_adapter_contract.py) verifies
route/static/value-free boundaries and intentionally performs no network,
browser launch, upload, fill, submission or confirmation. It does not establish
all seven dimensions of MJ-REQ-224.

The [controlled rehearsal](../services/applications/controlled_dry_run.py) has its
own Greenhouse/Lever/Workday-labelled contact, approved-answer and document flow.
Its receipts explicitly deny provider acceptance and disable submission; it does
not exercise each adapter's DOM/upload implementation. This is why the Workday
rehearsal remains valid while its adapter DOM/helper are unavailable.

The separate [fictional mock adapter](../services/applications/mock_adapter.py)
supports controlled navigation, approved-answer fill, upload, validation,
opt-in local mock submission, durable confirmation and uncertainty/replay
handling. Mock confirmation is not an employer receipt. MJ-REQ-224 remains
PARTIAL; real employer submissions remain OFF.

## Tests

These are executed results, not tests inferred from code presence.

| Revision/environment | Actual command or verification | Result |
|---|---|---|
| Web-security source `d096d03deba8aa749f478f9dd6c91029379ec50f`, isolated native worktree | `python -m pytest -q` over the ten explicitly listed paths in [source execution](evidence/2026-11-03-webpro-642e4b-web-security/source-execution.json) | **PASS: 151 tests + 33 subtests; 0 FAIL, 0 SKIP.** |
| Proposed merge `cbc172fadfb66e5cf661ca6eff667f57f1db379f`, tree `68712813bd808cb1b16572b26b2f4ab94ef52348` | Same ten-path scope in a separate detached worktree, with identical verified frontend assets | **PASS: 151 tests + 33 subtests; 0 FAIL, 0 SKIP.** |
| Workday source `1b8da8cf27c989a8a23e3733f6a9098fca5e00c3` | `python -m pytest -q tests/unit/test_workday_application_adapter.py tests/unit/test_fleet_t11_workday_dom_unavailable.py tests/unit/test_application_adapter_contract.py tests/integration/test_fleet_t04_lead_workday_documents.py` | **PASS: 55 tests + 23 subtests; 0 FAIL, 0 SKIP; 17.148 s.** |
| Web frontend in isolated source worktree | `npm ci --prefix apps/web --ignore-scripts --no-audit --no-fund`; `npm run typecheck --prefix apps/web`; `npm run build --prefix apps/web` | **PASS.** Existing native Node/npm compatibility warning and bundle-size advisory retained; dependencies unchanged. |
| Repository evidence consistency at web-security source/evidence checkpoint | `python -m tools.verification.catalogue`; `python -m tools.verification.progress validate`; `python -m tools.verification.progress_dashboard --check`; `git diff --check`; `python -m tools.verification.commit_provenance --commit HEAD` | **PASS.** These checks validate recorded structure/provenance, not complete feature behavior. |
| Hosted PR #893, exact head `c988d307ce0d8208e60886d83fb1cea59ccb7e79` | CI / required, PostgreSQL/Redis, disposable Compose, introduced-vulnerability review and secret scan | **PASS:** all five required gates; CodeQL also PASS. |
| Existing baseline backup | `python -m cli.local_backup verify --archive "$env:LOCALAPPDATA\MJobsMicro\backups\20261102T215244Z-821171122a44"` | **PASS: 3 files; PERSONAL; schema 2.** This verifies a prior-release archive, not a fresh backup of subsequent changes. |

Web-security hosted runs:
[CI](https://github.com/MulattoTech/MJobs/actions/runs/41573053502),
[PostgreSQL/Redis integration](https://github.com/MulattoTech/MJobs/actions/runs/41573053571),
[disposable Compose](https://github.com/MulattoTech/MJobs/actions/runs/41573053559),
[dependency review](https://github.com/MulattoTech/MJobs/actions/runs/41573053576),
[secret scan](https://github.com/MulattoTech/MJobs/actions/runs/41573053565).
The [protected merge receipt](evidence/2026-11-03-webpro-642e4b-web-security/protected-merge.json)
records actual main `ea5fbf9f0c3c27ecb0d86b90168f276df0349c6b`, with a tree
identical to the tested candidate. Deployment remained pending at integration.
Workday source receipts and independent review are preserved in
[its execution evidence](evidence/2026-11-03-webpro-642e4b-workday-contract/source-execution.json)
and [review](evidence/2026-11-03-webpro-642e4b-workday-contract/review.json).
Review was independent internal ChatGPT review, not a formal GitHub human approval.

Earlier failures remain recorded. The initial browser interception assumption
produced **FAIL: 1 failed / 12 passed** because Chromium restored metadata.
A test-only proxy now asserts both incoming and forwarded headers. After the
frontend build, an existing unknown-route assertion produced **FAIL: 1 failed /
150 passed / 33 subtests** because a mounted SPA returned 405 rather than 404.
The corrected test uses a stable real GET-only route; the final scope above
passes. These are corrected fixture assumptions, not erased test history.

**NOT RUN in this sprint/report:** a fresh opt-in live collector sweep, public
ATS uploads or submissions, paid AI inference, physical-host restart, destructive
production restore, a production rollback drill, fresh-install startup, and the
complete original MJ-REQ-292 acceptance journey. Existing broader source/matching/
document acceptances cited above are historical canonical evidence, not newly
executed tests. The report's containing PR records its own final candidate,
required gates and merge receipt without pretending those future checks ran at
this inventory checkpoint.

## External Limitations

CAPTCHA, MFA, authentication walls, anti-bot restrictions and required accounts
require supported human intervention; they are not bypassed. Changed provider
markup, ambiguous employer identity, unsupported mandatory questions and
conditional/repeated controls can stop automation for review. Source limits and
incomplete inventories remain explicit errors. WWR's conflicting upstream
identity cannot be resolved by silently discarding one distinct record.

The preview has no native multiuser authentication. Read-only Cloudflare
inspection found an email allow policy and an external-evaluation allow policy,
with HttpOnly enabled and tunnel Access validation required. The evaluator's
complete authorization behavior was not verified, so this report does not infer
an exclusive-owner authorization guarantee from the login screen. Anonymous
and invalid-session probes redirected to Access; the existing authenticated
Brave session loaded the app. No cookies, addresses or credentials are published.

The inspected candidate request-boundary implementation requires affirmative
same-origin context for public-host writes. Its headerless-local exception is a
request-header compatibility boundary; it does not authenticate a client as
native software. Complete
authentication/authorization and legacy-client coverage under MJ-REQ-172 are
not accepted. Network and service health observations do not prove remote
personal-document workflows.

## Deployment

### Subsequent verified rollout

The baseline observations below were superseded by the guarded PR #893 rollout
at **2026-11-03T04:27:55.591295+00:00**. The
[deployment receipt](evidence/2026-11-03-webpro-642e4b-implementation-report/runtime-followup.json)
and [protected PR record](https://github.com/MulattoTech/MJobs/pull/893#issuecomment-6595580184)
identify source `ea5fbf9f0c3c27ecb0d86b90168f276df0349c6b` and image
`sha256:5527283317257f4669dedf96d290e3f751e66d922e0896cd07d3e22fa9aa36ca`.
All seven application services were healthy on that image; readiness passed
PostgreSQL, Redis, scheduler, schema and all five worker roles. Five origin
boundary probes passed using POST on GET-only `/__deployment`, without invoking
a business mutation. Anonymous and synthetic-invalid sessions redirected to
Access. Authenticated Brave returned the same source SHA and rendered
**Services ready** with **External submission OFF**.

The fresh database/document archive independently passed verification. The
previous image and source were retained; a production restore or rollback drill
was **NOT_RUN**. The installed updater matched reviewed source and used its
native lock. The separately merged AI-resume revision `7e86511f` and pending
Workday/report changes were **not included in this observed rollout**. This is
a dated deployment observation, not a claim that production follows later main
automatically.

### Installation and earlier runtime baseline

For a **fresh** Windows installation, install Git and Docker Desktop, start
Docker Desktop in Linux-container mode, and run PowerShell:

~~~powershell
git clone https://github.com/MulattoTech/MJobs.git
cd MJobs
.\Start-MJobs.ps1
~~~

Open **http://127.0.0.1:18700** for the fictional FIXTURE workspace. Host Python
and Node are not required to use this Docker installation.

For a separate personal workspace:

~~~powershell
.\Start-MJobs.ps1 -Personal
~~~

Open **http://127.0.0.1:18701**. The default Compose projects are `mjobs` and
`mjobs-personal`, with separate persistent volumes. Do not change a populated
project's mode. Preserve an existing configuration, credential-encryption key,
database and document volumes during upgrades; never delete volumes to fix
startup.

The launcher supports `-NoOpen`, `-Project`, `-Port` and `-NoBuild`; use the latter
only for an already-built revision. It creates missing configuration atomically,
validates an existing encryption key, invokes Compose with readiness waiting and
polls `/readiness`. These startup commands were inspected here, **NOT RUN as a
fresh installation**. Full upgrade and backup instructions are in
[installation](INSTALLATION.md) and [the backup runbook](runbooks/LOCAL_BACKUP.md).

The existing remote preview uses Compose project `mjobs-micro` on the authorized
Windows Docker Desktop host, loopback **http://127.0.0.1:18801** and
**https://mjobs.m-techlabs.com** through Access/Tunnel. Its observed prechange
image was
`mjobs-micro:sha-63b843dfe2706e6a5ac1d567af7e5acb01e134a7`,
ID `sha256:b929faaf6cb5da407126085a812118c27aaa8ea7fad1596e830e9b4f6f63ebab`.
`/__deployment`, protected updater state and runtime agreed on that revision,
version `0.1.0-dev` and PERSONAL mode.

The existing ten-minute scheduled updater installs only current-main commits
with successful exact-SHA deployment-candidate, CI, PostgreSQL/Redis and secret
checks. It validates an independent release directory, refuses changed migration
sources, builds a SHA-tagged image, rechecks main/gates, quiesces only application
writers, creates/verifies a fresh database/document backup, and preserves volumes.
It records success only after exact deployment SHA and readiness verification;
failure attempts the previous image without an automatic database restore.

Manual updater execution also requires the sprint's separate deployment claim
and the updater's own nonblocking Windows byte lock. The installed script has
**no supported command-line flags**: `--help` or `--dry-run` would still execute
it. A zero exit can mean no eligible release or a busy lock, so state/runtime
must be checked. Use the existing [deployment runbook](../infra/deploy/README.md)
and [updater source](../infra/deploy/windows/update_mjobs_micro.py), not an
unverified feature branch or a second stack attached to the live hostname.

The prior archive `20261102T215244Z-821171122a44` verified read-only; schema
revision was `20261025di01`. No restore/rollback drill was performed. Off-PC
backup presence was not verified. Docker startup is configured at Windows user
sign-in; availability before sign-in is not guaranteed. This report's prechange
observation is not post-change deployment acceptance; final release evidence
belongs to the containing protected PR and sprint handoff.

## Known Limitations

| Requirement or boundary | Current limitation |
|---|---|
| 004 — bounded external operations | Accepted deadline slices do not establish every acquisition, connection, network and finalization boundary. Preserve remaining PARTIAL scope. |
| 008 — interruption recovery | Process/container/browser and PostgreSQL fencing evidence does not close physical-host restart, actual RQ maintenance redelivery, named Redis interruption opt-in or the complete interruption/journal contract. |
| 292 / 293 — original E2E and done | Narrow connected fixture journeys do not establish the entire original journey or all live-source criteria. Recorded states remain PARTIAL / NOT_RUN. |
| 155 / 264 — continuous pipeline and employer origin | Separate stages and narrow reciprocal-domain acquisition work; the complete continuously automated enrichment/application/analytics flow and general origin discovery remain incomplete. |
| 017 / 018 — sources and strategy | Five implemented collectors do not satisfy all provider/strategy breadth. WWR conflicting identity remains unresolved rather than silently merged. |
| 171 / 172 — privacy and web security | PII umbrella verification remains unexecuted; web-request repairs are PARTIAL. Native multiuser authentication, complete Access-evaluator authorization and headerless-local legacy behavior are not fully accepted. |
| 224 — adapter contract | Metadata/review, controlled rehearsals and mock journeys do not establish seven-dimension contracts across every adapter. Workday DOM/fixture upload remain unavailable. |
| 269 / 095 / 100 — documents | Reviewed CLI AI-resume proposals now preserve original fact references and attribution through accepted history. Broader tailoring and cover-letter scope remains incomplete; web drafting controls and automatic semantic verification are not supplied by this path. MJ-REQ-269's status follows its separate canonical receipt. |
| Matching/AI | Useful scoring/configuration components exist; broad matching/provider requirements remain PARTIAL, and MJ-REQ-118 (Application Priority) remains NOT_RUN. |
| Production readiness | The product is a developer preview. Healthy services, a protected merge, an architecture document or this report cannot accept all of 290/294/296–300. Real external submissions remain OFF. |

## Next Recommended Improvements

1. Complete the remaining bounded-operation and interruption-recovery criteria
   with deterministic failure injection and disposable environments; retain
   durable uncertainty instead of retrying non-idempotent work.
2. Extend the connected synthetic journey to the complete original MJ-REQ-292
   checklist and preserve exact PostgreSQL/Redis/browser/restart evidence.
3. Complete general employer-origin discovery under ownership, provenance and
   source-policy controls, building on the narrow Greenhouse path; then verify
   the continuous pipeline across saved-search and strong-match triggers.
4. Complete privacy and authorization verification, including Access external
   evaluation and the documented local-native request exception.
5. Add genuinely bounded, owned-browser ATS operations and per-adapter contract
   journeys. Do not restore the prior Workday DOM prototype without an
   independent watchdog, cancellation and no-egress proof.
6. Extend the reviewed CLI resume-draft path toward the remaining document
   requirements and supported web workflows. Verify any queued integration or
   additional document types with controlled provider fixtures, preserving
   explicit acceptance, profile/job scope, source hashes and historical versions.
7. Resolve source-specific reliability gaps with provenance, especially WWR's
   conflicting identity, and extend provider/strategy coverage only when the
   actual supported path is verified. A provider label or stale live result
   must not become fresh acceptance.
