# MJobs Area Specification — delivery_control

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-290, MJ-REQ-291, MJ-REQ-292, MJ-REQ-293, MJ-REQ-294, MJ-REQ-295, MJ-REQ-296, MJ-REQ-297, MJ-REQ-298, MJ-REQ-299, MJ-REQ-300

---

# 290. IMPLEMENTATION PHASES

Use the following as a detailed coverage checklist. The GPT-6 Execution Kernel controls delivery order and favors verified vertical slices over a rigid waterfall. Reorder or interleave these phases when doing so reduces integration risk, but do not omit required capabilities.

---

## PHASE A — SAFE BASELINE

Inspect:

```text
repository
existing architecture
Git state
dependencies
Docker
database
frontend
backend
tests
available host resources
```

Do not perform external application submissions.

---

## PHASE B — ARCHITECTURE REVIEW

Trace current architecture.

Determine:

```text
source boundary
persistence
task queue
frontend/backend boundary
document system
browser system
```

Document major decisions briefly.

Then implement.

Do not spend the entire task on architecture.

---

## PHASE C — CORE PLATFORM

Implement:

```text
FastAPI/backend
frontend
PostgreSQL
migrations
Redis/task system
settings
health
authentication if appropriate
Docker Compose
```

---

## PHASE D — CAREER PROFILE & DOCUMENT IMPORT

Implement:

```text
career profile
resume upload
resume extraction
career facts
document storage
```

---

## PHASE E — NORMALIZED JOB MODEL

Implement:

```text
companies
global sources
canonical jobs
job postings
application routes
search profiles
filters
```

---

## PHASE F — GLOBAL SOURCE FRAMEWORK

Implement:

```text
adapter interface
central HTTP client
scheduler
source health
raw storage
```

Then create several REAL usable adapters.

---

## PHASE G — COMPANY DISCOVERY

Implement:

```text
company identity
company aliases
domains
career-page discovery
ATS detector
ATS tenant model
CompanyJobSource
source enrollment
company-source scheduler
```

At least Greenhouse, Lever, and Ashby discovery should be genuinely implemented where their public interfaces permit it.

Add other providers where practical.

---

## PHASE H — DEDUPLICATION

Implement:

```text
strong-ID dedupe
company matching
title normalization
description similarity
duplicate confidence
canonical merge
source preservation
application-route dedupe
```

---

## PHASE I — SEARCH UI

Implement:

```text
Jobs
filters
sorting
job detail
source list
company detail
saved searches
```

---

## PHASE J — MATCHING

Implement:

```text
skill extraction
requirement classification
deterministic score
match explanation
priority score
```

---

## PHASE K — AI

Implement:

```text
provider abstraction
structured outputs
AI enrichment
cost controls
```

---

## PHASE L — DOCUMENT GENERATION

Implement:

```text
tailored resume
factual validation
DOCX/PDF
cover letters
versioning
diff
```

---

## PHASE M — ANSWER BANK

Implement:

```text
approved answers
question normalization
semantic matching
confidence
sensitive policies
```

---

## PHASE N — APPLICATION AUTOMATION FRAMEWORK

Implement:

```text
ApplicationAdapter
ATS routing
persistent browser profiles
generic form classifier
application state
attempts
step trace
```

---

## PHASE O — REAL ATS ADAPTERS

Implement several real application adapters.

Prioritize:

```text
Greenhouse
Lever
Ashby
```

then additional practical ATS systems.

Do not stop with stub classes.

---

## PHASE P — REVIEW / INTERVENTION

Implement:

```text
CAPTCHA pause
login pause
unknown question
sensitive question
resume workflow
```

---

## PHASE Q — AUTO-APPLY RULE ENGINE

Implement:

```text
rules
limits
confidence gates
source policies
global switches
```

---

## PHASE R — APPLICATION TRACKER

Implement:

```text
pipeline
history
notes
audit
confirmation
```

---

## PHASE S — DASHBOARD / ANALYTICS / DIAGNOSTICS

Complete:

```text
dashboard
source health
company discovery activity
automation queue
worker health
errors
basic analytics
```

---

## PHASE T — MOCK END-TO-END TESTING

Run:

```text
mock aggregator
company discovery
ATS discovery
employer enumeration
dedupe
scoring
document generation
application
confirmation
```

Fix failures.

---

## PHASE U — CONTROLLED LIVE DISCOVERY TEST

Test several real public sources read-only.

Do not submit real applications merely as tests.

---

## PHASE V — DEPLOYMENT VALIDATION

Run:

```text
Docker build
Docker Compose
migrations
health checks
worker checks
frontend build
```

---

## PHASE W — FINAL AUDIT

Search repository for:

```text
TODO
FIXME
pass
NotImplemented
placeholder
mock production data
hard-coded secrets
swallowed exceptions
dead code
unsafe debug behavior
```

Fix what is reasonable.

---


# 291. TEST MATRIX

At minimum test:

## Career Profile

```text
PDF import
DOCX import
fact review
skills
experience
education
```

## Search Profiles

```text
create
edit
delete
include/exclude
location
salary
remote
```

## Global Sources

```text
successful run
empty run
timeout
rate limit
malformed listing
duplicate listing
```

## Company Discovery

```text
new company
known company
alias
official domain
career page
custom ATS domain
multiple ATS portals
failure
rediscovery
```

## ATS Detection

```text
Greenhouse
Lever
Ashby
unknown
ambiguous
```

## Job Model

```text
normalize
dedupe
description changes
closed job
```

## Matching

```text
excellent
strong
weak
required missing
preferred missing
remote mismatch
salary mismatch
```

## Documents

```text
resume generated
cover letter generated
unsupported claim detected
version saved
```

## Applications

```text
form fill
upload
question
conditional field
validation
review pause
CAPTCHA pause
submission
confirmation
uncertain submission
```

## Idempotency

```text
duplicate source run
duplicate company discovery
duplicate application queue
concurrent submission
```

## Recovery

```text
worker crash
browser crash
scheduler restart
submission uncertainty
```

## Security

```text
XSS
SSRF
malicious URL
path traversal
malicious filename
injection-like text
```

---


# 292. END-TO-END ACCEPTANCE TEST

I should be able to:

1. Start the full stack.
2. Open the dashboard.
3. Complete first-run setup.
4. Upload my resume.
5. Review extracted career information.
6. Correct any parsing errors.
7. Save the verified career profile.
8. Create a remote IT search profile.
9. Enable several job sources.
10. Run discovery.
11. See real discovered jobs.
12. Search them.
13. Filter them.
14. Sort by match.
15. See salary where available.
16. See remote restrictions.
17. See duplicate source counts.
18. Open a canonical job.
19. See every known source copy.
20. See the preferred authoritative source.
21. See its application route.
22. See an explainable match score.
23. Encounter an employer not already monitored.
24. See company discovery run automatically.
25. See the official company domain identified.
26. See the career site identified.
27. See the ATS identified where possible.
28. See the employer enrolled as a direct source.
29. See all current employer openings enumerated.
30. See an employer-only relevant job that was not in the original aggregator.
31. See duplicate employer/aggregator postings merge correctly.
32. Save or shortlist a strong match.
33. Generate a truthful tailored resume.
34. Compare it to the base resume.
35. Generate a unique cover letter.
36. Prepare an application.
37. See the application route selected.
38. Have the correct ATS adapter selected.
39. Open the application in Playwright.
40. Have contact fields filled.
41. Have resume uploaded.
42. Have cover letter uploaded if applicable.
43. Have approved questions answered.
44. Have an uncertain question stop for review.
45. Approve/correct it.
46. Resume the application.
47. Validate the entire form.
48. In dry-run, stop before actual external submission.
49. Against the local mock ATS, submit.
50. Detect confirmation.
51. Persist application history.
52. See the application on the tracker.
53. See all relevant audit events.
54. Restart the stack.
55. Confirm jobs remain.
56. Confirm company sources remain.
57. Confirm documents remain.
58. Confirm applications remain.
59. Confirm scheduler resumes.
60. Confirm company monitoring resumes.

---


# 293. DEFINITION OF DONE

The application is NOT complete merely because:

```text
Docker starts
the UI renders
PostgreSQL exists
a job table exists
source interfaces exist
one scraper works
resume generation works
Playwright launches
```

It is complete when the major workflow functions coherently end-to-end.

Specifically:

```text
multi-source discovery works
automatic company career-site discovery works
ATS detection works
direct employer monitoring works
normalization works
deduplication works
search/filter works
matching works
resume tailoring works
cover letters work
answer bank works
application preparation works
at least several real ATS adapters work
review workflow works
submission confirmation works in controlled tests
tracking works
recovery works
24/7 scheduling works
Docker persistence works
```

---


# 294. REQUIRED DELIVERABLES

Deliver the actual implementation.

At minimum:

1. Workspace assessment.
2. Architecture assessment.
3. Complete backend.
4. Complete frontend.
5. PostgreSQL schema.
6. Migrations.
7. Durable background worker system.
8. Scheduler.
9. Search profiles.
10. Global source abstraction.
11. Several working job-source adapters.
12. Company identity system.
13. Company alias system.
14. Domain-resolution system.
15. Automatic career-site discovery.
16. ATS detector.
17. ATS tenant model.
18. Automatic CompanyJobSource enrollment.
19. Direct employer monitoring.
20. Job normalization.
21. Canonical job model.
22. Multi-source posting model.
23. Application-route model.
24. Deduplication engine.
25. Full-text search.
26. Advanced filters.
27. Match scoring.
28. Match explanations.
29. Career-profile system.
30. Resume import.
31. Career fact validation.
32. Document library.
33. Tailored resume engine.
34. Cover-letter engine.
35. Answer bank.
36. AI provider abstraction.
37. Application adapter architecture.
38. Greenhouse application support.
39. Lever application support.
40. Ashby application support where practical.
41. Generic form automation.
42. Persistent browser profiles.
43. Application state machine.
44. Application idempotency.
45. Review queue.
46. CAPTCHA/manual-intervention workflow.
47. Application tracker.
48. Automation rules.
49. Application limits.
50. Global automation controls.
51. Audit logs.
52. Diagnostics.
53. Source-health dashboard.
54. Company-source dashboard.
55. Worker health.
56. Basic analytics.
57. Backup tooling.
58. Restore procedure.
59. Docker Compose.
60. Health checks.
61. Unit tests.
62. Integration tests.
63. Browser tests.
64. Mock aggregator.
65. Mock company website.
66. Mock ATS.
67. Complete end-to-end CI test.
68. Documentation.
69. Verification utility.
70. Final implementation report.

---


# 295. FINAL IMPLEMENTATION REPORT

At completion provide a compact but thorough report.

Include:

## Architecture

What was built and why.

## Files

What was:

```text
added
modified
removed
```

## Services

Report status for:

```text
frontend
backend
database
Redis
scheduler
workers
browser automation
```

## Sources

For every implemented global source:

```text
source
strategy
working status
limitations
```

## Company Discovery

Report:

```text
domain resolution
career discovery
ATS detectors
company source enrollment
tested providers
```

## Deduplication

Describe:

```text
strong identifiers
semantic methods
thresholds
application-route dedupe
```

## Matching

Describe deterministic and AI components.

## Documents

Report:

```text
resume parsing
resume tailoring
factual validation
cover letters
formats
```

## Applications

For each supported ATS:

```text
autofill
upload
question handling
submit capability
confirmation
limitations
```

## Tests

Report actual commands and results.

Never fabricate:

```text
PASS
```

If a test was not run:

state:

```text
NOT RUN
```

If it failed:

state:

```text
FAIL
```

and explain why.

## External Limitations

Examples:

```text
CAPTCHA
required account
source restrictions
changed site structure
unsupported mandatory question
```

## Deployment

Provide exact startup instructions.

## Known Limitations

Be explicit.

## Next Recommended Improvements

Prioritize only meaningful remaining work.

---


# 296. IMPORTANT FAILURE RULE

If a source or ATS changes during implementation:

do not remove the entire subsystem.

Investigate:

```text
API
public endpoint
HTML
structured data
ATS underneath
browser
```

Then choose the cleanest supported approach.

If no stable approach is available:

mark that integration appropriately and continue building the rest of the system.

---


# 297. IMPORTANT AUTONOMY RULE

You have authority to:

```text
design the architecture
create the project
refactor existing code
replace poor partial architecture
create database schemas
create migrations
create job-source adapters
create company-discovery adapters
create ATS detectors
create browser automation
create workers
create tests
create Docker configuration
create UI
create APIs
create documentation
install reasonable project dependencies
run migrations against development/test environments
run mock browser applications
run read-only public job-source tests
```

You do NOT have authority to:

```text
fabricate user credentials
fabricate career experience
invent demographic answers
solve CAPTCHAs through circumvention
bypass MFA
defeat access controls
perform anti-bot evasion
submit random real job applications merely as tests
modify unrelated host drivers
modify firmware
reboot the workstation without explicit permission
reset unrelated host networking
destroy unrelated user data
```

If implementation appears to require one of those prohibited actions:

> **CHANGE THE IMPLEMENTATION APPROACH.**

---


# 298. CORE DESIGN PRINCIPLE

The final architecture should feel like:

```text
The application manages a Career Profile.

The Career Profile defines truthful, verified facts.

Search Profiles define what jobs are wanted.

Global Job Sources discover opportunities.

Discovered employers become candidates for direct monitoring.

Company Discovery finds official employer career infrastructure.

ATS Detection determines how employers publish and accept applications.

Company Job Sources continuously monitor first-party careers pages.

All source postings feed one normalized pipeline.

Canonical Jobs eliminate duplicate clutter.

Application Routes eliminate duplicate submissions.

Matching determines relevance.

Document generation creates truthful tailored materials.

The Answer Bank provides verified reusable answers.

Application Adapters interact with supported ATS systems.

Uncertain questions become review items.

CAPTCHAs and authentication become resumable user-intervention items.

Successful submissions become durable Application records.

Workers and schedulers run continuously.

Every important operation is persistent and recoverable.

The user controls automation.

The user does not manually orchestrate the infrastructure.
```

---


# 299. PRODUCT OUTCOME

The intended real-world experience is:

> I install the application, upload my resume, verify my background, tell it what kinds of jobs I want, and enable the sources I want searched. From then on it continuously discovers jobs from major remote-job sources and directly monitored employer career pages. When it encounters a promising employer it does not already know, it automatically identifies that employer's official careers infrastructure, detects the ATS where possible, adds the employer as a first-party source, checks all of that employer's current openings, and continues watching that company in the future. It groups duplicate listings into one clean job record while preserving every source. It scores jobs against my actual experience and explains why they match. It can generate truthful tailored resumes and unique cover letters. It can automatically prepare supported applications, fill forms, upload documents, answer previously approved questions, pause when human review is genuinely required, and—when I explicitly enable the policy—submit high-confidence supported applications. It tracks everything it discovered, generated, attempted, submitted, and learned. It runs continuously without me manually checking ten different job websites every day.

That is the product.

---


# 300. START EXECUTION

Start with the workspace inspection phase.

Determine:

1. whether an existing repository/application is present,
2. the current architecture,
3. the current Git/change state,
4. the existing persistence model,
5. current frontend/backend technology,
6. current tests,
7. current deployment method,
8. whether any useful partial job-source/application code already exists.

Then establish or update the implementation journal, select the smallest coherent vertical slice that advances the current milestone, and proceed with implementation and verification.

Do not stop after producing another plan.

Do not hand me a list of files I should create.

Do not tell me how I could build the application.

Build it.

Implement real source integrations.

Implement automatic employer career-site discovery.

Implement real ATS detection.

Implement direct company-source monitoring.

Implement normalization and deduplication.

Implement matching.

Implement resume and cover-letter generation.

Implement application automation.

Implement persistent background processing.

Implement tests.

Run the tests.

Fix the failures.

Run the application.

Validate the Docker deployment.

Perform the complete local mock end-to-end workflow.

Perform bounded read-only testing of appropriate real job sources.

Leave the repository in the strongest functional, documented, recoverable state the environment permits.

And above all:

# DO NOT TREAT AUTOMATIC COMPANY CAREER-SITE DISCOVERY AS AN OPTIONAL OR LATER FEATURE.

It is a first-class part of the job-discovery architecture.

A third-party job listing should be capable of teaching this system where that employer actually publishes jobs, after which the platform should monitor that first-party source directly and continually improve its own discovery network.
---

# GPT-6 CODEX CLOSING EXECUTION DIRECTIVE

Begin by inspecting the actual workspace and establishing the evidence-backed baseline.

Then **build the product, not another description of the product**.

Use the detailed catalogue as the requirements checklist and the GPT-6 Execution Kernel as the execution method.

Keep the repository working after every meaningful checkpoint.

Prefer one verified vertical slice over ten disconnected half-built subsystems.

Prefer deterministic truth over AI guesswork.

Prefer first-party authoritative data over aggregator assumptions while preserving provenance.

Prefer durable state over fragile speed.

Prefer a safe review stop over an invented answer.

Prefer `SUBMISSION_UNCONFIRMED` over a duplicate application.

Prefer an accurately reported external blocker over a fake PASS.

Continue until the strongest implementation and verification state permitted by the environment has been reached.

At the end, leave:

- working code,
- migrations,
- tests,
- fixtures,
- Docker/deployment assets,
- documentation,
- the machine-readable implementation journal,
- verification evidence,
- an explicit capability matrix,
- and a concise final implementation report that distinguishes PASS, FAIL, PARTIAL, BLOCKED, and NOT RUN.

Do not perform real external application submission without the user's explicit enablement and normal product policy gates.

Do not weaken safety or truthfulness to make the project appear complete.
