# MJobs Area Specification — operations_ux_ci

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-248, MJ-REQ-249, MJ-REQ-250, MJ-REQ-251, MJ-REQ-252, MJ-REQ-253, MJ-REQ-254, MJ-REQ-255, MJ-REQ-256, MJ-REQ-257, MJ-REQ-258, MJ-REQ-259, MJ-REQ-260, MJ-REQ-261, MJ-REQ-262, MJ-REQ-263, MJ-REQ-264, MJ-REQ-265, MJ-REQ-266, MJ-REQ-267, MJ-REQ-268, MJ-REQ-269, MJ-REQ-270, MJ-REQ-271, MJ-REQ-272, MJ-REQ-273, MJ-REQ-274, MJ-REQ-275, MJ-REQ-276, MJ-REQ-277, MJ-REQ-278, MJ-REQ-279, MJ-REQ-280

---

# 248. HEALTH ENDPOINTS

Provide:

```text
/health
/ready
```

Check:

```text
database
queue
worker
```

Optionally expose browser-worker status separately.

---


# 249. DOCKER HEALTH CHECKS

Configure appropriate container health checks.

Do not mark backend healthy merely because its process exists.

---


# 250. FIRST-RUN WIZARD

A polished flow:

```text
1 Create profile
2 Upload resume
3 Review extracted facts
4 Configure desired jobs
5 Configure location/remote preferences
6 Configure application preferences
7 Configure AI
8 Enable sources
9 Choose automation level
10 Run first search
```

---


# 251. FIRST-RUN RESUME REVIEW

Do not start automatic applications before the user has had the opportunity to verify imported career facts.

---


# 252. SEARCH PROFILE SEED

Provide an example profile for remote IT infrastructure.

Example only:

```text
System Administrator
Systems Administrator
Windows Administrator
IT Administrator
Infrastructure Support
Endpoint Administrator
Microsoft 365 Administrator
Systems Engineer
Technical Support Engineer
NOC Engineer
```

---


# 253. DEFAULT TECHNOLOGY SEED

Example optional preferred skills:

```text
Active Directory
Windows Server
PowerShell
Microsoft 365
Entra ID
Azure
Group Policy
VMware
Hyper-V
DNS
DHCP
Intune
SCCM
Rubrik
SAN
NAS
```

Do not hard-code them into generic matching logic.

---


# 254. UX STANDARD

The final system should feel:

```text
buttery smooth
crisp
intentional
predictable
transparent
recoverable
```

That means:

```text
good defaults
little unnecessary configuration
fast filtering
meaningful progress
clear automation state
useful errors
safe retry
persistent operations
explanations
no mysterious frozen screens
```

---


# 255. RESPONSIVE DESIGN

Primary target:

```text
desktop web
```

but basic mobile/tablet usability should remain reasonable.

---


# 256. ACCESSIBILITY

Use:

```text
semantic HTML
proper labels
keyboard access
ARIA where appropriate
```

This also improves automated testing.

---


# 257. LIGHT / DARK MODE

Implement if it integrates cleanly.

Do not prioritize it over application functionality.

---


# 258. DO NOT OVERENGINEER

Do not introduce:

```text
Kubernetes
service mesh
distributed consensus
complex event sourcing
microservice sprawl
Kafka
enterprise orchestration
```

without genuine need.

A clean Docker Compose architecture is sufficient initially.

---


# 259. FUTURE-PROOF WITHOUT BUILDING EVERYTHING NOW

Architecture should permit future additions:

```text
email parsing
Gmail integration
Outlook integration
recruiter tracking
interview scheduling
interview prep
salary negotiation
browser extension
native desktop wrapper
mobile notifications
additional ATS providers
additional job sources
```

These must not block core completion.

---


# 260. OPTIONAL EMAIL STATUS INGESTION

If straightforward, architecture may include an email integration boundary for detecting:

```text
application confirmation
rejection
interview invitation
recruiter outreach
offer
```

Do not make this mandatory for the initial complete workflow.

---


# 261. SOURCE VS ATS SEPARATION

Always preserve:

```text
DISCOVERY SOURCE
```

versus:

```text
APPLICATION PROVIDER
```

Example:

```text
Discovered:
HiringCafe

Application:
Greenhouse

Employer:
Acme
```

These are different concepts.

---


# 262. SOURCE RELATIONSHIP GRAPH

The data model should effectively support:

```text
Company
   │
   ├── CompanyJobSource
   │       │
   │       └── JobPosting
   │
CanonicalJob
   │
   ├── JobPosting
   │
   └── ApplicationRoute
           │
           └── Application
```

---


# 263. COMPANY DISCOVERY QUEUE

Use a separate queue.

Company discovery should not synchronously delay every job import.

---


# 264. PRIORITY COMPANY DISCOVERY

If:

```text
match score >= configured threshold
```

and company has no direct source:

enqueue high-priority company discovery.

---


# 265. CONFIGURABLE COMPANY-DISCOVERY THRESHOLDS

Conceptual settings:

```text
company_discovery_enabled
minimum_match_for_discovery
priority_match_for_discovery
max_new_company_discoveries_per_hour
```

---


# 266. APPLICATION ROUTE PREFERENCE

Default preferred route hierarchy when identity is known:

```text
verified direct employer / official ATS
    ↓
trusted integrated job board
    ↓
aggregator
```

Do not universally assume the first-party route is always technically superior; preserve configurable policy.

---


# 267. DIRECT EMPLOYER ROUTE DISCOVERY BEFORE APPLY

Before an automatic submission through an aggregator:

attempt lightweight resolution of whether that listing ultimately routes to the employer ATS.

Do not delay indefinitely.

---


# 268. APPLICATION STATUS TRACKING

The user should be able to manually set:

```text
Applied
Recruiter Contact
Interview
Offer
Rejected
Withdrawn
```

Automatic email-based status updates can be added later.

---


# 269. DOCUMENT FACT TRACE

For AI-generated documents, optionally retain references to the career facts used.

This improves explainability.

---


# 270. AI PROMPT VERSIONING

Store internal prompt/template versions for:

```text
resume
cover letter
screening answers
job parsing
```

This makes generated output reproducible.

---


# 271. AI FAILURE BEHAVIOR

AI outages must not stop discovery.

Example:

```text
job discovery continues
deterministic scoring continues
AI-dependent operations queue/retry
```

---


# 272. DOCUMENT GENERATION FAILURE

If resume tailoring fails:

do not submit using an incomplete/corrupt file.

Mark:

```text
DOCUMENT_GENERATION_FAILED
```

---


# 273. APPLICATION WORKER FAILURE

A crashed application worker must not cause automatic resubmission without reconciliation.

---


# 274. PROCESS RESTART RECOVERY

On service restart:

```text
scheduler resumes
workers reconnect
pending discovery continues
company sources persist
documents persist
application state persists
ambiguous submissions remain ambiguous
```

---


# 275. CONFIGURATION

Do not require source-code editing for ordinary settings.

UI/environment configuration should cover:

```text
AI
sources
source intervals
company discovery
match thresholds
application rules
application limits
browser profiles
documents
notifications
logging
```

---


# 276. SETTINGS VERSIONING

Configuration schema should be versioned where appropriate.

Migrate old values safely.

---


# 277. IMPORT / EXPORT SETTINGS

Allow:

```text
search profiles
automation rules
source configuration
```

to be exported/imported as validated JSON.

Do not include secrets by default.

---


# 278. CI

Create CI that runs without external job websites.

At minimum:

```text
backend lint
backend type check
backend tests
frontend lint
frontend type check
frontend tests
frontend build
mock E2E
```

---


# 279. PYTHON QUALITY

Prefer:

```text
Ruff
pytest
type hints
mypy or pyright where practical
```

---


# 280. TYPESCRIPT QUALITY

Use:

```text
strict TypeScript
ESLint
formatter
component tests
```

---
