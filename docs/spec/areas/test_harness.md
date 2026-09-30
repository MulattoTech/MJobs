# MJobs Area Specification — test_harness

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-208, MJ-REQ-209, MJ-REQ-210, MJ-REQ-211, MJ-REQ-212, MJ-REQ-213, MJ-REQ-214, MJ-REQ-215, MJ-REQ-216, MJ-REQ-217, MJ-REQ-218, MJ-REQ-219, MJ-REQ-220, MJ-REQ-221, MJ-REQ-222, MJ-REQ-223, MJ-REQ-224, MJ-REQ-225, MJ-REQ-226, MJ-REQ-227, MJ-REQ-228, MJ-REQ-229, MJ-REQ-230

---

# 208. DRY-RUN MODE

Implement:

```text
DRY_RUN
```

In dry-run:

```text
discovery works
company discovery works
scoring works
documents generate
browser navigates
forms fill
validation runs
```

but final external submission does not occur.

This is essential.

---


# 209. MOCK SOURCE ENVIRONMENT

Create a local fake job board.

It should expose:

```text
multiple jobs
duplicate jobs
salary variants
remote variants
application redirects
unknown company
```

---


# 210. MOCK COMPANY ENVIRONMENT

Create a fictional employer:

```text
Example Systems Inc.
```

with:

```text
homepage
careers navigation
career portal
ATS redirect
multiple jobs
```

---


# 211. MOCK ATS ENVIRONMENT

Create local mock ATS application flows.

Include:

```text
simple one-page form
multi-page form
resume upload
cover-letter upload
dropdowns
radio buttons
checkboxes
custom questions
conditional questions
validation errors
confirmation screen
```

---


# 212. AUTOMATIC COMPANY DISCOVERY TEST

The automated test must prove:

```text
mock aggregator finds Example Systems Job A
↓
company not previously known
↓
official company site identified
↓
career page found
↓
ATS detected
↓
CompanyJobSource created
↓
current employer jobs fetched
↓
Job A matched to aggregator duplicate
↓
Employer-only Job B discovered
```

---


# 213. FULL APPLICATION E2E TEST

The test must then continue:

```text
Job B
↓
normalized
↓
scored
↓
resume generated
↓
cover letter generated
↓
application queued
↓
mock ATS detected
↓
Playwright opens form
↓
fields populated
↓
resume uploaded
↓
questions answered
↓
validated
↓
submitted
↓
confirmation detected
↓
database updated
```

This is a required CI-level demonstration.

---


# 214. UNIT TESTS — NORMALIZATION

Test:

```text
titles
companies
locations
remote restrictions
salary
dates
employment type
experience
skills
education
clearance
```

---


# 215. UNIT TESTS — DEDUPE

Include:

```text
exact duplicate
same ATS requisition
slightly different title
changed salary
different job same company
same title different city
boilerplate-heavy descriptions
```

---


# 216. UNIT TESTS — COMPANY RESOLUTION

Test:

```text
company aliases
different punctuation
subsidiaries
similarly named unrelated companies
domain evidence
ATS evidence
```

---


# 217. UNIT TESTS — ATS DETECTION

Test:

```text
direct ATS host
custom branded domain
redirect
embedded ATS
unknown portal
ambiguous portal
```

---


# 218. UNIT TESTS — MATCHING

Test:

```text
required skill
preferred skill
missing skill
remote mismatch
salary mismatch
seniority mismatch
education preferred vs required
```

---


# 219. UNIT TESTS — ANSWER MATCHING

Test:

```text
exact
normalized
fuzzy
semantic
ambiguous
sensitive
```

---


# 220. UNIT TESTS — APPLICATION STATE

Test:

```text
valid transitions
invalid transitions
retry
CAPTCHA pause
login pause
submission uncertainty
confirmed submission
```

---


# 221. APPLICATION IDEMPOTENCY TEST

Prove that two workers cannot submit the same route concurrently.

---


# 222. SOURCE ADAPTER CONTRACT TESTS

Every adapter should satisfy standardized checks:

```text
returns jobs
stable source IDs
valid URLs
parses dates
parses descriptions
does not crash on missing fields
```

---


# 223. COMPANY SOURCE CONTRACT TESTS

Every company/ATS discovery adapter should validate:

```text
tenant
company identity
job enumeration
stable job IDs
application URL
```

---


# 224. APPLICATION ADAPTER CONTRACT TESTS

Each application adapter should prove:

```text
ATS detection
navigation
field fill
upload
question handling
validation
confirmation
```

against controlled fixtures.

---


# 225. SECURITY TESTS

At minimum:

```text
XSS in description
SQL injection-like strings
malicious filename
path traversal
SSRF URL
malformed redirect
HTML injection
oversized upload
invalid MIME
AI malformed JSON
```

---


# 226. APPLICATION FORM SECURITY TESTS

Application fields may contain:

```text
quotes
Unicode
ampersands
hyphens
apostrophes
```

Treat them as data.

Never construct executable commands from application input.

---


# 227. TEST RESOURCE LIMITS

Do not run:

```text
hundreds of browsers
unbounded worker pools
unbounded AI calls
```

during testing.

Functional verification is sufficient.

---


# 228. LIVE SOURCE TESTING

After mocks pass:

run bounded read-only discovery against several real sources.

Verify:

```text
jobs collected
normalization
dedupe
company extraction
source health
```

Do not submit applications during this phase.

---


# 229. LIVE COMPANY DISCOVERY TEST

Use one or several publicly discoverable employer career sites.

Verify:

```text
careers page
ATS detection
job enumeration
```

No submission required.

---


# 230. LIVE APPLICATION TEST POLICY

Do not submit a random real employer application merely to test software.

Real submission requires:

```text
actual user-intended job
user-enabled real submission
validated materials
normal application policy
```

Use the mock ATS for routine CI.

---
