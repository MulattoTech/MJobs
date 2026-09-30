# MJobs Area Specification — application_automation

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-121, MJ-REQ-122, MJ-REQ-123, MJ-REQ-124, MJ-REQ-125, MJ-REQ-126, MJ-REQ-127, MJ-REQ-128, MJ-REQ-129, MJ-REQ-130, MJ-REQ-131, MJ-REQ-132, MJ-REQ-133, MJ-REQ-134, MJ-REQ-135, MJ-REQ-136, MJ-REQ-137, MJ-REQ-138, MJ-REQ-139, MJ-REQ-140, MJ-REQ-141, MJ-REQ-142, MJ-REQ-143, MJ-REQ-144, MJ-REQ-145, MJ-REQ-146, MJ-REQ-147, MJ-REQ-148, MJ-REQ-149, MJ-REQ-150, MJ-REQ-151, MJ-REQ-152, MJ-REQ-153, MJ-REQ-154

---

# 121. APPLICATION AUTOMATION ARCHITECTURE

Create a first-class:

```text
ApplicationAdapter
```

abstraction.

The adapter should operate on a normalized:

```text
ApplicationContext
```

containing:

```text
user profile
job
application route
resume
cover letter
answer bank
browser profile
automation policy
```

---


# 122. ATS APPLICATION ADAPTERS

Prioritize practical reusable adapters.

Initial priority:

```text
Greenhouse
Lever
Ashby
SmartRecruiters
Workable
Recruitee
Teamtailor
Breezy
BambooHR
generic HTML
```

Then add more difficult providers as practical:

```text
Workday
iCIMS
Jobvite
SuccessFactors
Taleo
Dayforce
```

Do not leave the initial high-priority adapters as empty skeletons.

---


# 123. AUTOMATION MODES

Support:

```text
AUTO_SUBMIT
REVIEW_BEFORE_SUBMIT
ASSISTED
MANUAL
```

---


# 124. DEFAULT AUTOMATION POLICY

First-run default:

```text
Discovery:
ON

Scoring:
ON

Resume tailoring:
ON

Cover-letter generation:
ON

Form preparation:
ON

Final unattended submission:
OFF
```

The user explicitly enables final unattended submission.

---


# 125. SOURCE-SPECIFIC SUBMISSION POLICY

Example:

```text
Greenhouse
  autofill=true
  autosubmit=true

Lever
  autofill=true
  autosubmit=true

Workday
  autofill=true
  autosubmit=false

Unknown
  autofill=true
  autosubmit=false
```

User configurable.

---


# 126. BROWSER PROFILE MANAGEMENT

Support persistent Playwright browser contexts.

The user may manually sign into services when required.

Store:

```text
profile_id
provider/site
status
last_used
likely_authenticated
created_at
```

Do not log cookies.

---


# 127. CREDENTIAL STORAGE

Never place:

```text
passwords
cookies
tokens
API secrets
session credentials
```

into normal logs.

Use server-side secrets and appropriate file permissions.

---


# 128. GENERIC FORM ENGINE

Create a reusable form-analysis engine.

Inspect:

```text
label
name
id
placeholder
ARIA label
input type
nearby text
section heading
fieldset legend
dropdown options
```

---


# 129. STANDARD FIELD TAXONOMY

Classify:

```text
first_name
middle_name
last_name
full_name

email
phone

address
city
state
postal_code
country

linkedin
github
portfolio

current_company
current_title

work_authorization
sponsorship

desired_salary
travel
relocation
remote

education
experience

resume_upload
cover_letter_upload
```

---


# 130. FORM MATCHING FALLBACK ORDER

Use:

```text
1 stable adapter-specific selectors
2 labels
3 ARIA roles
4 semantic DOM
5 surrounding text
6 controlled fuzzy matching
7 visual/OCR fallback
```

---


# 131. OCR

OCR is a fallback.

Do not use OCR when the information exists in:

```text
DOM
accessibility tree
form metadata
```

OCR may help where custom rendered interfaces make text otherwise inaccessible.

---


# 132. MULTI-PAGE APPLICATIONS

Support:

```text
Next
Continue
Save & Continue
previous page
review page
submit page
```

Maintain application state across pages.

---


# 133. DYNAMIC FORMS

Handle:

```text
conditional questions
dependent dropdowns
new fields appearing after answers
optional sections
repeatable experience rows
```

without assuming every form is static.

---


# 134. FILE UPLOAD

Support reliable upload of:

```text
resume
cover letter
supporting documents
```

Validate the correct document was selected for the correct job.

---


# 135. APPLICATION VALIDATION

Before submission verify:

```text
required fields complete
correct identity
correct contact information
correct resume
correct cover letter
known questions resolved
no mandatory field errors
no unexpected sensitive question
no CAPTCHA
automation policy permits submission
```

---


# 136. APPLICATION PREVIEW

Provide a review screen showing:

```text
Job
Company
Route
Resume
Cover Letter
All answers
Outstanding issues
Automation confidence
```

---


# 137. APPLICATION STATE MACHINE

Use explicit states.

Example:

```text
DISCOVERED
SHORTLISTED
DOCUMENTS_PENDING
DOCUMENTS_READY
READY_TO_APPLY
QUEUED
STARTING
NAVIGATING
FILLING
WAITING_FOR_REVIEW
USER_INTERVENTION_REQUIRED
VALIDATING
SUBMITTING
SUBMISSION_UNCONFIRMED
SUBMITTED
CONFIRMED
FAILED
RETRY_PENDING
REJECTED
INTERVIEW
OFFER
WITHDRAWN
ARCHIVED
```

Prevent invalid transitions.

---


# 138. APPLICATION ATTEMPT MODEL

An application may have multiple attempts.

Store:

```text
attempt_id
application_id
route_id
started_at
finished_at
state
browser_session
adapter
current_step
error_code
confirmation
```

---


# 139. APPLICATION STEP TRACE

Record:

```text
NAVIGATE
DETECT_FORM
UPLOAD_RESUME
FILL_CONTACT
FILL_LOCATION
ANSWER_STANDARD
ANSWER_CUSTOM
VALIDATE
SUBMIT
VERIFY_CONFIRMATION
```

---


# 140. CONFIDENCE GATES

Auto-submit only when:

```text
adapter confidence >= configured threshold
field classification confidence acceptable
no unresolved required question
documents validated
duplicate-submission check passes
source policy permits
user rule permits
```

---


# 141. CAPTCHA BEHAVIOR

On detection:

```text
CAPTCHA_REQUIRED
```

Then:

```text
pause
preserve session
notify user
show Resume button
```

Do not abandon the entire application if the user can continue later.

---


# 142. AUTHENTICATION BEHAVIOR

If login expires:

```text
AUTH_REQUIRED
```

Preserve application progress where possible.

Allow user to authenticate manually and resume.

---


# 143. APPLICATION CONFIRMATION

Detect:

```text
Thank you for applying
Application received
We've received your application
confirmation ID
application reference
```

Capture:

```text
timestamp
confirmation URL
confirmation text
reference number
```

---


# 144. AMBIGUOUS SUBMISSION

If the browser fails immediately after clicking Submit:

do not blindly retry.

Set:

```text
SUBMISSION_UNCONFIRMED
```

Attempt read-only verification before resubmission.

---


# 145. APPLICATION IDEMPOTENCY

Prevent:

```text
Worker A submits
Worker B submits same requisition
```

Use:

```text
database locks
unique constraints
task idempotency keys
application-route identity
```

---


# 146. APPLICATION ROUTE CANONICALIZATION

Canonical application identity may be:

```text
company
ATS provider
ATS tenant
requisition ID
```

This is often stronger than the source listing URL.

---


# 147. APPLY THROUGH ALL SAFETY

If user selects:

```text
Apply through All Sources
```

first resolve destinations.

Example:

```text
Dice          ┐
HiringCafe    ├─→ Greenhouse Req 123
RemoteRocket ┘

Company page ─→ Greenhouse Req 123
```

Submit once.

Explain why four source listings resulted in one application.

---


# 148. DISTINCT APPLICATION CHANNELS

If evidence proves genuinely different submission systems:

permit separate applications if user policy allows.

Track each separately.

---


# 149. AUTOMATIC APPLICATION RULE ENGINE

Allow rules such as:

```text
match_score >= 85
remote = true
salary >= minimum OR salary unknown
experience_level not senior+
blacklisted_company = false
clearance_required = false
```

---


# 150. AUTO-SUBMIT RULE

Example:

```text
match_score >= 92
AND application_adapter_confidence >= 0.96
AND required_questions_resolved = true
AND sensitive_review_required = false
AND captcha = false
AND duplicate_application = false
AND source_autosubmit_enabled = true
```

---


# 151. APPLICATION LIMITS

Configurable:

```text
max_auto_submissions_per_hour
max_auto_submissions_per_day
max_applications_per_company_per_day
max_application_routes_per_canonical_job
minimum_match_score
```

Use conservative defaults.

---


# 152. AUTOMATION GLOBAL SWITCHES

Provide:

```text
Discovery            ON/OFF
Company Discovery    ON/OFF
AI Processing        ON/OFF
Document Generation  ON/OFF
Application Prep     ON/OFF
Auto Submit          ON/OFF
```

Prominently display current state.

---


# 153. PAUSE / RESUME

Users must be able to pause:

```text
all automation
specific queues
specific sources
specific companies
specific applications
```

without losing state.

---


# 154. QUIET HOURS

Optional quiet-hour policy for:

```text
browser workers
notifications
high-CPU tasks
```

Discovery may continue if configured.

---
