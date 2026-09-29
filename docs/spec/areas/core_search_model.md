# MJobs Area Specification — core_search_model

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-015, MJ-REQ-016, MJ-REQ-017, MJ-REQ-018, MJ-REQ-019, MJ-REQ-020, MJ-REQ-021, MJ-REQ-022, MJ-REQ-023, MJ-REQ-024, MJ-REQ-025, MJ-REQ-026, MJ-REQ-027, MJ-REQ-028, MJ-REQ-029, MJ-REQ-030, MJ-REQ-031, MJ-REQ-032, MJ-REQ-033, MJ-REQ-034, MJ-REQ-035, MJ-REQ-036, MJ-REQ-037, MJ-REQ-038, MJ-REQ-039

---

# 15. DOMAIN MODEL — CORE CONCEPTS

The architecture must distinguish at minimum:

```text
User / CareerProfile
SearchProfile
Company
CompanyAlias
CompanyDomain
JobSource
CompanyJobSource
CanonicalJob
JobPosting
ApplicationRoute
JobMatch
Document
Application
ApplicationAttempt
AnswerBankEntry
BrowserProfile
AutomationRule
WorkerOperation
AuditEvent
```

Do not collapse all of these into one `jobs` table.

---


# 16. GLOBAL JOB SOURCES

Create a first-class source adapter interface.

Conceptually:

```text
JobSourceAdapter

search()
fetch()
normalize()
health()
capabilities()
```

or a cleaner equivalent.

The rest of the application must not contain source-specific scraping logic.

---


# 17. INITIAL GLOBAL SOURCE TARGETS

Prioritize sources useful for remote jobs and technology roles.

Desired integrations include where technically practical:

```text
Dice
We Work Remotely
Wellfound
HiringCafe
Working Nomads
Remote Rocketship
Remote OK
Remotive
Built In
```

Also investigate appropriate publicly available sources discovered during implementation.

Architecture should allow future adapters for:

```text
LinkedIn
Indeed
ZipRecruiter
Glassdoor
Monster
government job systems
specialized niche boards
```

Do not implement unsupported anti-bot workarounds merely to claim source coverage.

---


# 18. SOURCE COLLECTION STRATEGY

For each source prefer, in order:

```text
1. documented official API
2. documented public feed
3. RSS / Atom
4. public structured JSON endpoint
5. structured HTML
6. browser rendering
7. accessibility/DOM interpretation
8. OCR only if truly necessary
```

Do not use a browser where a clean public structured endpoint exists.

---


# 19. SOURCE CAPABILITY MODEL

Each source must declare capabilities similar to:

```text
DISCOVERY
JOB_DETAIL
SALARY
REMOTE_METADATA
APPLICATION_REDIRECT
DIRECT_APPLICATION
APPLICATION_STATUS
```

and collection strategy:

```text
API
FEED
PUBLIC_JSON
PUBLIC_HTML
BROWSER
ASSISTED
MANUAL
DISABLED
```

---


# 20. SOURCE POLICY

Maintain configurable per-source policy:

```text
enabled
poll_interval
concurrency
request_timeout
rate_limit
last_attempt
last_success
consecutive_failures
backoff_until
health_status
collection_strategy
```

One failed source must not stop the pipeline.

---


# 21. CONTINUOUS SCHEDULING

Support search intervals such as:

```text
15 minutes
30 minutes
1 hour
2 hours
6 hours
12 hours
daily
custom
```

Do not require all sources to use the same cadence.

Use:

```text
retry
backoff
jitter
rate limiting
```

---


# 22. SEARCH PROFILES

Users must be able to create unlimited search profiles.

Example:

```text
Name:
Remote Infrastructure / SysAdmin

Include Titles:
System Administrator
Systems Administrator
Windows Administrator
Infrastructure Support
Microsoft 365 Administrator
Endpoint Administrator
Systems Engineer

Remote:
Required

Employment:
Full-time
```

Search profiles must be data-driven.

---


# 23. SEARCH QUERY LANGUAGE

Support configurable:

```text
include titles
exclude titles
required keywords
optional keywords
excluded keywords
exact phrases
technology aliases
company include/exclude
```

Where useful permit:

```text
AND
OR
NOT
```

semantics.

Regex may be exposed as an advanced feature.

---


# 24. LOCATION FILTERING

Support:

```text
remote
hybrid
onsite
country
state
city
radius
remote US
remote worldwide
remote specific states
```

Do not treat all "remote" listings as universally remote.

---


# 25. REMOTE CLASSIFICATION

Normalize into values such as:

```text
REMOTE_WORLDWIDE
REMOTE_US
REMOTE_COUNTRY
REMOTE_SPECIFIC_STATES
HYBRID
ONSITE
UNKNOWN
```

Extract restrictions.

Example:

```text
Remote, but applicants must reside in:
TX, OK, AR, KS
```

should not be normalized simply as unrestricted remote.

---


# 26. EMPLOYMENT TYPE

Normalize:

```text
FULL_TIME
PART_TIME
CONTRACT
CONTRACT_TO_HIRE
TEMPORARY
INTERNSHIP
OTHER
UNKNOWN
```

---


# 27. EXPERIENCE LEVEL

Normalize:

```text
INTERNSHIP
ENTRY
JUNIOR
ASSOCIATE
MID
SENIOR
LEAD
MANAGER
DIRECTOR
EXECUTIVE
UNKNOWN
```

Preserve original language.

---


# 28. SALARY FILTERING

Support:

```text
minimum salary
maximum salary
hourly
annual
known only
unknown allowed
```

Normalize values while preserving raw source text.

---


# 29. SALARY PARSER

Parse forms including:

```text
$60,000–$75,000
$32–38/hour
$70k+
Up to $95,000
Starting at $65k
$30/hr
```

Store:

```text
salary_min
salary_max
salary_period
currency
salary_source
```

Differentiate:

```text
EMPLOYER_PROVIDED
SOURCE_PROVIDED
ESTIMATED
UNKNOWN
```

Never present an estimate as employer-provided compensation.

---


# 30. DATE FILTERING

Support:

```text
last hour
today
24 hours
3 days
7 days
30 days
custom
```

Normalize relative timestamps.

Store both:

```text
reported_posted_at
first_seen_at
last_seen_at
```

---


# 31. SKILL FILTERING

Support normalized technologies.

Examples:

```text
Active Directory
Windows Server
PowerShell
Microsoft 365
Entra ID
Azure
AWS
Linux
VMware
Hyper-V
DNS
DHCP
Group Policy
Intune
SCCM
MECM
Cisco
Docker
Kubernetes
Terraform
Ansible
Veeam
Rubrik
SAN
NAS
PACS
```

No technology should be hard-coded as globally required.

---


# 32. SKILL TAXONOMY

Support aliases.

For example:

```text
AD
Active Directory
Microsoft Active Directory
```

may map to:

```text
ACTIVE_DIRECTORY
```

Similarly:

```text
O365
Office 365
M365
Microsoft 365
```

map appropriately.

Preserve original wording.

---


# 33. TITLE NORMALIZATION

Normalize equivalent titles without losing original titles.

Example:

```text
Sys Admin
System Admin
Systems Administrator
Windows Sysadmin
```

may map to a canonical family.

Do not over-collapse genuinely different positions.

---


# 34. EDUCATION REQUIREMENT PARSING

Differentiate:

```text
Bachelor's required
Bachelor's preferred
Bachelor's or equivalent experience
Degree preferred
No degree specified
```

These are materially different.

---


# 35. EXPERIENCE REQUIREMENT PARSING

Extract:

```text
minimum_years
maximum_years
preferred_years
requirement_strength
```

from text such as:

```text
1–2 years
2+ years
3–5 years
5 years preferred
equivalent experience accepted
```

---


# 36. CERTIFICATION PARSING

Recognize certifications generically.

Examples may include:

```text
CompTIA A+
Network+
Security+
CCNA
AZ-900
AZ-104
ITIL
MCSA
MCSE
```

Classify:

```text
REQUIRED
PREFERRED
NICE_TO_HAVE
```

---


# 37. CLEARANCE FILTERS

Identify:

```text
No clearance stated
Public Trust
Secret
Top Secret
TS/SCI
Other
```

Allow search profiles to exclude clearance requirements.

---


# 38. WORK AUTHORIZATION FILTERS

Extract statements such as:

```text
US citizenship required
Sponsorship available
No sponsorship
Must already be authorized
```

Do not infer the user's own authorization.

That belongs in explicit career-profile configuration.

---


# 39. USER NEGATIVE FILTERS

Support configurable exclusions such as:

```text
travel > 25%
on-call
night shift
1099
commission
door-to-door
clearance required
mandatory relocation
```

Do not assume these preferences.

---
