# MJobs Area Specification — governance_safety

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-000, MJ-REQ-001, MJ-REQ-002, MJ-REQ-003, MJ-REQ-004, MJ-REQ-005, MJ-REQ-006, MJ-REQ-007, MJ-REQ-008, MJ-REQ-009, MJ-REQ-010, MJ-REQ-011, MJ-REQ-012, MJ-REQ-013, MJ-REQ-014

---

# 0. HIGHEST-PRIORITY DIRECTIVES

The following rules override every other instruction in this prompt.

## ACTUALLY IMPLEMENT THE APPLICATION

Do not respond with only:

```text
an architecture proposal
a roadmap
pseudocode
a list of recommended technologies
UI mockups
empty interfaces
TODO comments
placeholder service classes
```

You may begin by inspecting, planning, and documenting the architecture.

Then implement it.

Do not stop after planning.

The objective is a functioning application.

---

## PRESERVE TRUTH IN JOB APPLICATIONS

The system must NEVER fabricate:

```text
employment history
job titles
employment dates
degrees
certifications
skills
technologies
projects
responsibilities
accomplishments
security clearances
licenses
years of experience
work authorization
salary history
demographic answers
```

Generated resumes, cover letters, and application answers may:

```text
rephrase
reorder
emphasize
summarize
condense
expand truthful context
select relevant facts
```

but they may not invent qualifications.

If a job requires something the user's verified career profile does not contain:

```text
Missing requirement: AWS Lambda
```

is acceptable.

Inventing AWS Lambda experience is not.

---

## AUTOMATION MUST NOT BECOME AN UNCONTROLLED SPAM BOT

The application should support high automation.

It should not blindly submit thousands of low-quality applications.

Implement:

```text
match thresholds
daily limits
hourly limits
company limits
duplicate prevention
quality gates
confidence gates
sensitive-question gates
source-specific policies
```

Automation should optimize:

```text
relevance
speed
consistency
coverage
```

not indiscriminate volume.

---

## NO CAPTCHA OR ACCESS-CONTROL BYPASS

Do not implement:

```text
CAPTCHA solving
CAPTCHA outsourcing
Cloudflare bypass
anti-bot evasion
fingerprint spoofing
stealth browser frameworks whose primary purpose is evasion
rate-limit circumvention
credential bypass
MFA bypass
access-control bypass
```

If automation reaches such a barrier:

```text
USER_INTERVENTION_REQUIRED
```

must be the result.

The user should be able to complete the challenge manually and resume the application workflow afterward.

---

## AUTOMATION IS STILL A CORE REQUIREMENT

The previous restriction does NOT authorize avoiding browser automation.

You must actually implement:

```text
form recognition
field classification
resume uploads
cover-letter uploads
question answering
multi-page form handling
validation
submission where enabled
confirmation detection
persistent browser sessions
application recovery
```

for supported application systems.

---


# 1. PROTECTED HOST / DEVELOPMENT SAFETY

Treat the user's workstation as a production-like machine.

Building this application does not require altering low-level host configuration.

Do not:

```text
upgrade graphics drivers
downgrade graphics drivers
reinstall graphics drivers
modify BIOS/UEFI
update firmware
reset host networking
disable network adapters
run broad driver updates
perform broad package upgrades unrelated to the project
modify GPU tuning
change TDR settings
reboot the host without explicit authorization
kill unrelated applications to free resources
```

Do not use broad operations such as:

```text
winget upgrade --all
choco upgrade all
```

or equivalents merely because development dependencies are missing.

Install only project-specific dependencies when needed.

---


# 2. NO GPU REQUIREMENT

This project does not require GPU acceleration.

Keep it that way unless the repository already contains an explicitly user-configured optional local AI path.

Default architecture:

```text
CPU
RAM
disk
network
headless browser
```

Do not:

```text
benchmark the GPU
stress the GPU
modify GPU drivers
configure GPU passthrough
require CUDA
require ROCm
```

just to build the application.

AI processing may use external APIs or optional local endpoints.

The core system must not depend upon a GPU.

---


# 3. HEADLESS DEVELOPMENT WHERE POSSIBLE

Browser automation is central to the product, but development/testing should prefer:

```text
Playwright headless Chromium
local mock sites
fixture HTML
controlled browser concurrency
```

Do not launch dozens of interactive browsers.

Do not intentionally exercise WebGL/WebGPU.

Browser testing should verify:

```text
navigation
DOM parsing
field detection
uploads
multi-step forms
validation
submission
confirmation
authentication persistence
```

not graphics performance.

---


# 4. BOUND EXTERNAL OPERATIONS

Every operation that may hang should use:

```text
timeouts
captured output
controlled cancellation
retry policy
error classification
```

This includes:

```text
HTTP requests
browser navigation
browser selectors
AI requests
document parsing
OCR
external processes
source collectors
ATS collectors
```

Never leave workers permanently blocked on one source.

---


# 5. CONTROL CONCURRENCY

Use deliberate concurrency.

Suitable parallelism:

```text
multiple lightweight source fetches
several parsing tasks
several scoring tasks
controlled AI tasks
```

Conservative concurrency:

```text
browser application workers
document generation
OCR
company-site discovery
```

Do not spawn an uncontrolled number of Chromium instances.

Configure independent concurrency limits for:

```text
source_collection
company_discovery
AI
document_generation
application_automation
```

---


# 6. CURRENT WORKSPACE INSPECTION

Begin by inspecting the current workspace.

Determine:

```text
whether a repository already exists
repository structure
language/framework
existing database code
existing frontend
existing backend
existing browser automation
existing tests
existing Docker configuration
existing environment files
existing CI
```

If relevant existing work exists:

classify it as:

```text
KEEP
REFACTOR
REPLACE
REMOVE
```

Do not overwrite good work blindly.

If no application exists:

create it from scratch.

---


# 7. SOURCE CONTROL / CHANGE SAFETY

If Git exists and is healthy:

use it normally.

Inspect:

```text
git status
current branch
uncommitted modifications
```

Do not discard unrelated user changes.

If no normal Git repository exists:

do not initialize Git merely for convenience without evaluating context.

Instead create a lightweight change manifest recording:

```text
file
original hash
new hash
backup location
operation
timestamp
```

Critical configuration/database migrations must be recoverable.

---


# 8. SAVE IMPLEMENTATION PROGRESS

Maintain a machine-readable implementation journal such as:

```text
outputs/job-platform-implementation-state.json
```

containing:

```text
current_phase
completed_steps
files_added
files_modified
tests_completed
tests_remaining
migrations_applied
live_integrations_tested
known_errors
known_limitations
last_safe_checkpoint
```

Update this after meaningful milestones.

The work must be resumable after:

```text
terminal interruption
Codex interruption
container restart
host restart
browser crash
worker crash
```

---


# 9. SEPARATE DEVELOPMENT FROM PRODUCTION OPERATION

Use:

```text
SOURCE
   ↓
IMPLEMENTATION
   ↓
STATIC VALIDATION
   ↓
UNIT TESTS
   ↓
MOCKED INTEGRATION TESTS
   ↓
BROWSER FIXTURE TESTS
   ↓
CONTROLLED LIVE SOURCE READ TESTS
   ↓
DRY-RUN APPLICATION TEST
   ↓
OPTIONAL USER-ENABLED REAL APPLICATION
   ↓
DEPLOYMENT
```

Do not develop by continuously submitting real employment applications.

Real external submission is not a test harness.

---


# 10. PRIMARY PRODUCT ARCHITECTURE

The product should conceptually resemble:

```text
                    WEB DASHBOARD
                          │
                          ▼
                    APPLICATION API
                          │
      ┌───────────────────┼────────────────────┐
      │                   │                    │
      ▼                   ▼                    ▼
 SEARCH SERVICE     APPLICATION SERVICE   PROFILE/DOC SERVICE
      │                   │                    │
      ▼                   ▼                    ▼
 SOURCE SYSTEM       AUTOMATION ENGINE      AI ENGINE
      │                   │                    │
      ├─────────┐         │                    │
      ▼         ▼         ▼                    ▼
 GLOBAL      COMPANY    PLAYWRIGHT        DOCUMENT ENGINE
 SOURCES     SOURCES    WORKERS
      │         │
      └────┬────┘
           ▼
     NORMALIZATION
           │
           ▼
      COMPANY RESOLUTION
           │
           ▼
      DEDUPLICATION
           │
           ▼
       JOB SCORING
           │
           ▼
        POSTGRES
```

Background operations must be durable.

---


# 11. PREFERRED TECHNOLOGY STACK

Unless the existing repository strongly justifies another mature architecture, prefer:

## Backend

```text
Python 3.12+
FastAPI
Pydantic
SQLAlchemy 2.x
Alembic
```

## Frontend

```text
TypeScript
React
Next.js
```

or another mature React architecture.

## Database

```text
PostgreSQL
```

Recommended extensions where useful:

```text
pg_trgm
pgvector
```

Do not require pgvector if simpler similarity methods satisfy the initial implementation.

## Background Processing

Prefer a durable queue using something such as:

```text
Celery + Redis
Dramatiq + Redis
RQ + Redis
```

Choose based on repository fit and reliability.

## Browser Automation

```text
Playwright
Chromium
```

## Deployment

```text
Docker Compose
```

---


# 12. SERVICE ARCHITECTURE

A reasonable deployment may contain:

```text
frontend
backend
scheduler
general-worker
source-worker
company-discovery-worker
AI-worker
application-browser-worker
postgres
redis
```

You may consolidate workers initially if appropriate.

However, preserve logical queue separation.

Browser workers should not block job discovery.

---


# 13. LOCAL-FIRST / SELF-HOSTED

The primary deployment must be self-hostable.

The user should not need a proprietary hosted backend.

Core data should remain under user control.

At minimum:

```text
PostgreSQL data
uploaded resumes
generated resumes
cover letters
application history
company-source catalog
configuration
```

must persist locally.

---


# 14. COMPLETE STARTUP EXPERIENCE

The final repository should support a startup path approximately like:

```bash
docker compose up -d
```

followed by a functioning application.

Provide:

```text
.env.example
Docker Compose
database migrations
health checks
first-run bootstrap
README
development commands
testing commands
backup commands
restore documentation
```

Validate these commands.

Do not merely assume they work.

---
