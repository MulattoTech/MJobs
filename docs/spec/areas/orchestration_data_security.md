# MJobs Area Specification — orchestration_data_security

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-155, MJ-REQ-156, MJ-REQ-157, MJ-REQ-158, MJ-REQ-159, MJ-REQ-160, MJ-REQ-161, MJ-REQ-162, MJ-REQ-163, MJ-REQ-164, MJ-REQ-165, MJ-REQ-166, MJ-REQ-167, MJ-REQ-168, MJ-REQ-169, MJ-REQ-170, MJ-REQ-171, MJ-REQ-172, MJ-REQ-173, MJ-REQ-174, MJ-REQ-175, MJ-REQ-176, MJ-REQ-177, MJ-REQ-178, MJ-REQ-179, MJ-REQ-180

---

# 155. CONTINUOUS PIPELINE

The complete automated pipeline should resemble:

```text
SCHEDULER
   ↓
GLOBAL SOURCES + KNOWN COMPANY SOURCES
   ↓
RAW POSTINGS
   ↓
NORMALIZATION
   ↓
COMPANY RESOLUTION
   ↓
CANONICAL DEDUPE
   ↓
UNKNOWN COMPANY?
   ├── YES → CAREER-SITE DISCOVERY
   │            ↓
   │        ATS DETECTION
   │            ↓
   │      COMPANY SOURCE ENROLLMENT
   │            ↓
   │      EMPLOYER JOB ENUMERATION
   │            ↓
   │      NORMALIZE / DEDUPE
   │
   ▼
DETERMINISTIC FILTERING
   ↓
MATCH SCORING
   ↓
AI ENRICHMENT IF WARRANTED
   ↓
AUTO-APPLY RULES
   ↓
SHORTLIST
   ↓
TAILORED RESUME
   ↓
COVER LETTER
   ↓
APPLICATION ANSWERS
   ↓
APPLICATION ROUTE RESOLUTION
   ↓
ATS ADAPTER
   ↓
FORM FILL
   ↓
VALIDATION
   ↓
AUTO SUBMIT / REVIEW / ASSISTED
   ↓
CONFIRMATION
   ↓
TRACKING
   ↓
ANALYTICS
```

---


# 156. PERSISTENT OPERATIONS

Long-running actions must not depend on one HTTP request remaining open.

Represent operations:

```text
operation_id
operation_type
entity_id
state
current_step
progress
started_at
updated_at
completed_at
error
retry_count
recovery_metadata
```

---


# 157. OPERATION TYPES

Examples:

```text
SEARCH_SOURCE
DISCOVER_COMPANY
VALIDATE_COMPANY_SOURCE
ENUMERATE_COMPANY
PARSE_JOB
DEDUPE_JOB
SCORE_JOB
GENERATE_RESUME
GENERATE_COVER_LETTER
PREPARE_APPLICATION
SUBMIT_APPLICATION
BACKUP
```

---


# 158. OPERATION RECOVERY

After worker restart:

```text
inspect persisted state
determine whether operation completed
reconcile external state
resume safe operations
avoid duplicate submission
```

---


# 159. TASK IDEMPOTENCY

Use deterministic task identifiers where appropriate.

Example:

```text
collect:<source>:<window>
company-discover:<company_id>
score:<canonical_job>:<profile_version>
apply:<route_id>:<career_profile_version>
```

---


# 160. RETRIES

Retry transient errors:

```text
HTTP timeout
temporary 5xx
temporary browser crash
AI timeout
queue disconnect
```

Do not blindly retry:

```text
CAPTCHA_REQUIRED
AUTH_REQUIRED
JOB_CLOSED
UNKNOWN_MANDATORY_QUESTION
SUBMISSION_UNCONFIRMED
```

---


# 161. ERROR CATEGORIES

Use structured errors:

```text
NETWORK_ERROR
RATE_LIMITED
SOURCE_CHANGED
SOURCE_BLOCKED
AUTH_REQUIRED
CAPTCHA_REQUIRED
ATS_UNSUPPORTED
FORM_CHANGED
FIELD_UNKNOWN
UPLOAD_FAILED
VALIDATION_FAILED
JOB_CLOSED
SUBMISSION_UNCONFIRMED
AI_ERROR
DOCUMENT_ERROR
```

---


# 162. DATABASE DESIGN

Use a proper relational model.

Likely tables include:

```text
users
career_profiles
career_facts
work_experience
education
certifications
skills
projects
answer_bank

search_profiles
search_queries
search_filters

companies
company_aliases
company_domains
company_relationships

job_sources
company_job_sources
source_runs
company_discovery_runs
company_discovery_evidence
ats_providers
ats_tenants

canonical_jobs
job_postings
job_posting_versions
application_routes

job_skills
job_requirements
job_locations

job_match_scores
job_match_components

documents
resume_versions
cover_letter_versions

application_rules
applications
application_attempts
application_answers
application_documents
application_events

browser_profiles

operations
audit_logs
notifications
settings
```

Exact names may differ.

---


# 163. DATABASE CONSTRAINTS

Use:

```text
foreign keys
unique constraints
not-null constraints
enum/check constraints where appropriate
```

Prevent impossible state where practical.

---


# 164. DATABASE INDEXES

Review actual query patterns.

Likely indexes:

```text
canonical job status
company
normalized title
posted_at
first_seen_at
remote type
salary
match score
application state
source
ATS
company source priority
```

---


# 165. FULL-TEXT SEARCH

Implement search over:

```text
title
company
description
skills
location
```

Use PostgreSQL full-text search and/or trigram support where appropriate.

---


# 166. DATA MIGRATIONS

Use Alembic or equivalent.

Before destructive migrations:

```text
back up
migrate
validate
```

Do not delete/recreate the database to make schema changes easier.

---


# 167. DATA BACKUP

Provide backup tooling for:

```text
database
uploaded documents
generated documents
important configuration
```

---


# 168. RESTORE

Create a documented and tested restore procedure.

A backup feature without a restore path is incomplete.

---


# 169. DATA EXPORT

Support:

```text
CSV jobs
CSV applications
JSON export
complete backup
```

---


# 170. RETENTION

Automatically clean bounded transient data such as:

```text
debug screenshots
temporary HTML
old raw payloads
temporary browser artifacts
temporary generated files
rotated logs
```

Do not delete core application history automatically.

---


# 171. SECURITY — PII

This application stores sensitive employment data.

Protect:

```text
contact information
resume content
employment history
application answers
session data
```

---


# 172. SECURITY — WEB

Implement appropriate:

```text
CSRF protection
secure cookies
input validation
output encoding
authentication
authorization
XSS protection
```

Sanitize third-party job-description HTML before rendering.

---


# 173. SECURITY — UPLOADS

Validate:

```text
MIME
extension
size
filename
storage path
```

Prevent:

```text
path traversal
executable uploads being treated as documents
filename collisions
```

---


# 174. SECURITY — DATABASE

Use:

```text
ORM
parameterized queries
least-privileged credentials
```

Never concatenate raw job content into SQL.

---


# 175. SECURITY — SECRET MANAGEMENT

Secrets belong in:

```text
environment variables
secret files
appropriate server-side stores
```

not:

```text
source code
browser HTML
logs
Git fixtures
```

---


# 176. AUDIT LOG

Maintain user-facing audit history.

Events:

```text
JOB_DISCOVERED
COMPANY_DISCOVERED
COMPANY_SOURCE_ENROLLED
JOB_MERGED
MATCH_SCORED
RESUME_GENERATED
COVER_LETTER_GENERATED
APPLICATION_QUEUED
APPLICATION_STARTED
APPLICATION_SUBMITTED
APPLICATION_CONFIRMED
APPLICATION_FAILED
SETTING_CHANGED
```

---


# 177. STRUCTURED LOGGING

Operational logs should include appropriate context:

```text
timestamp
operation_id
job_id
posting_id
company_id
application_id
source
ATS
worker
duration
result
error_code
```

Never log secret values.

---


# 178. LOG RETENTION

Implement rotation.

Do not allow verbose Playwright logs or raw HTML to fill the disk indefinitely.

---


# 179. DEBUG CAPTURE

On browser failure optionally capture:

```text
screenshot
sanitized DOM
current URL
console errors
network errors
current automation step
```

with retention limits.

---


# 180. REDACT DEBUG DATA

Avoid storing:

```text
passwords
cookies
sensitive demographic answers
tokens
```

in screenshots/HTML snapshots where possible.

---
