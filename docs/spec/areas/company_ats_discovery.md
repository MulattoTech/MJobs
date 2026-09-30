# MJobs Area Specification — company_ats_discovery

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-054, MJ-REQ-055, MJ-REQ-056, MJ-REQ-057, MJ-REQ-058, MJ-REQ-059, MJ-REQ-060, MJ-REQ-061, MJ-REQ-062, MJ-REQ-063, MJ-REQ-064, MJ-REQ-065, MJ-REQ-066, MJ-REQ-067, MJ-REQ-068, MJ-REQ-069, MJ-REQ-070, MJ-REQ-071, MJ-REQ-072, MJ-REQ-073, MJ-REQ-074, MJ-REQ-075, MJ-REQ-076, MJ-REQ-077, MJ-REQ-078, MJ-REQ-079, MJ-REQ-080, MJ-REQ-081, MJ-REQ-082, MJ-REQ-083, MJ-REQ-084

---

# 54. AUTOMATIC COMPANY CAREER-SITE DISCOVERY IS CORE

This is a **NON-OPTIONAL PRIMARY FEATURE**.

Whenever a useful job exposes a company not already directly monitored, the system should attempt to discover that employer's official recruiting infrastructure.

This is NOT a future enhancement.

It must be part of the initial architecture and implementation.

---


# 55. COMPANY DISCOVERY FLYWHEEL

The intended behavior:

```text
Aggregator discovers job
        ↓
New employer recognized
        ↓
Official employer domain resolved
        ↓
Career page discovered
        ↓
Underlying ATS detected
        ↓
Employer source automatically enrolled
        ↓
All current employer jobs enumerated
        ↓
Relevant jobs enter normal pipeline
        ↓
Employer monitored continuously
```

Over time, the system should develop a growing first-party source network.

---


# 56. COMPANY DISCOVERY TRIGGERS

Queue company discovery when:

```text
a previously unseen company appears
a high-match job appears
the user saves a company
the user marks a company preferred
the user manually adds a company
an application redirect exposes a new ATS tenant
a company repeatedly appears on aggregators
a known company source breaks
```

---


# 57. COMPANY IDENTITY RESOLUTION

Create a dedicated company-resolution service.

Potential signals:

```text
company name
normalized name
known aliases
website URL
application domain
ATS tenant metadata
job source metadata
first-party links
company domain
```

Return:

```text
company_id
confidence
evidence
```

---


# 58. COMPANY ALIASES

Model:

```text
Company
   └── CompanyAlias[]
```

Aliases may include:

```text
legal name
brand name
abbreviation
former name
source-specific spelling
```

---


# 59. COMPANY DOMAINS

Model verified and candidate domains.

Example fields:

```text
company_id
domain
confidence
verified
verification_method
first_seen_at
last_verified_at
```

---


# 60. OFFICIAL DOMAIN DISCOVERY

Prefer evidence in approximately this order:

```text
first-party job link
verified existing company record
ATS company metadata
official career redirect
structured site metadata
configured web-search provider
public company metadata
```

Do not confuse:

```text
LinkedIn
Crunchbase
Indeed
staffing agency
directory
aggregator
```

with the employer's official domain.

---


# 61. CAREER PAGE DISCOVERY

Once the company domain is known, locate likely career infrastructure.

Inspect:

```text
navigation
footer
sitemap
structured metadata
internal links
```

and common routes such as:

```text
/careers
/jobs
/careers/jobs
/jobs/search
/work-with-us
/join-us
/join-our-team
/employment
/opportunities
```

Do not rely solely on guessed URL paths.

---


# 62. EXTERNAL SEARCH PROVIDER ABSTRACTION

Company discovery may optionally use a configured search provider.

Create something such as:

```text
WebDiscoveryProvider
```

Do not hard-code one search engine.

Potential implementations:

```text
search API
custom search endpoint
disabled
```

The system must still work reasonably without one.

---


# 63. ATS AUTO-DETECTION

Implement an extensible:

```text
ATSDetector
```

Detect major recruiting systems where practical, including:

```text
Greenhouse
Lever
Ashby
SmartRecruiters
Workable
Workday
Jobvite
BambooHR
Recruitee
Teamtailor
Breezy
iCIMS
Oracle/Taleo
SuccessFactors
Dayforce
ADP Recruiting
JazzHR
ApplicantPro
UKG
Paycom
Paylocity
NEOGOV / GovernmentJobs
```

Do not require all of these to have complete auto-application support initially.

Detection and application support are separate capabilities.

---


# 64. ATS DETECTION SIGNALS

Use:

```text
hostname
redirect chain
URL structure
HTML signatures
script URLs
embedded widget configuration
structured metadata
public API patterns
form signatures
network-visible public endpoints
```

---


# 65. CUSTOM-DOMAIN ATS DETECTION

Do not assume ATS systems always use obvious domains.

Example:

```text
careers.acme.com
```

may be backed by Greenhouse.

The system should identify:

```text
Visible Careers Site:
careers.acme.com

Underlying ATS:
Greenhouse
```

when evidence supports it.

---


# 66. ATS TENANT MODEL

Where publicly discoverable, store:

```text
provider
tenant_identifier
organization_identifier
board identifier
```

Example conceptually:

```text
ATS: Lever
Tenant: acme
```

This permits efficient enumeration later.

---


# 67. COMPANY JOB SOURCE

Model first-party employer sources independently from global job boards.

Example:

```text
CompanyJobSource

company_id
ats_provider
ats_tenant
career_url
search_url
feed_url
public_api_url
collection_strategy
confidence
enabled
priority
discovered_automatically
last_validation
last_success
```

A company may have more than one source.

---


# 68. MULTIPLE CAREER PORTALS PER COMPANY

Support:

```text
Acme Corporate → Workday
Acme Federal → iCIMS
Acme Engineering → Greenhouse
Acme University Recruiting → custom portal
```

Do not assume:

```text
one company = one ATS
```

---


# 69. PARENT / SUBSIDIARY RELATIONSHIPS

Allow:

```text
parent_company_id
relationship_type
```

Do not merge companies only because they share:

```text
one corporate parent
one ATS
one domain
```

unless evidence supports identity equivalence.

---


# 70. MACHINE-READABLE EMPLOYER JOB ENDPOINTS

Once a career site is identified, prefer:

```text
official/public ATS endpoint
structured JSON
public board API
RSS/Atom
structured HTML
browser
```

in that order.

Avoid browser automation for simple data collection when a clean endpoint exists.

---


# 71. AUTOMATIC SOURCE ENROLLMENT

After validating a discovered employer career source:

persist it automatically.

Example:

```text
HiringCafe finds:
Acme – Windows Administrator

↓
official domain:
acme.com

↓
Careers:
careers.acme.com

↓
ATS:
Ashby

↓
CompanyJobSource created

↓
Acme is now monitored directly
```

---


# 72. IMMEDIATE EMPLOYER ENUMERATION

Do not wait until tomorrow's scheduler cycle.

When a new first-party employer source is discovered:

immediately perform one bounded current-job enumeration.

Any relevant jobs found must enter the regular pipeline.

---


# 73. FIRST-PARTY-ONLY JOB DISCOVERY

A critical acceptance case:

```text
Aggregator exposes Job A
↓
Company source discovered
↓
Company source contains:
Job A
Job B
Job C
↓
Job B and Job C were never found by aggregator
↓
B and C become discoverable
```

This feature proves the discovery flywheel is working.

---


# 74. COMPANY SOURCE PRIORITY

Automatically discovered companies should receive priority based on:

```text
number of relevant jobs
average match score
frequency of new jobs
application success
interview outcomes
user preference
recency
```

Example classes:

```text
USER_PINNED
HIGH
NORMAL
LOW
DORMANT
```

---


# 75. AUTO-DISCOVERED VS USER-WATCHED

Store:

```text
AUTO_DISCOVERED
USER_WATCHED
```

User-watched companies should not be silently demoted.

Automatically discovered sources may be reprioritized.

---


# 76. COMPANY SOURCE REVALIDATION

Career infrastructure changes.

Periodically verify:

```text
domain
career URL
ATS provider
tenant
feed/API
parser
```

If the source changes unexpectedly:

queue rediscovery.

---


# 77. ATS MIGRATION DETECTION

Example:

```text
Company was Greenhouse
↓
Source begins failing
↓
career URL redirects
↓
Workday detected
↓
new CompanyJobSource configuration validated
↓
source history retained
```

Do not delete the previous history.

---


# 78. COMPANY DISCOVERY CONFIDENCE

Use confidence scores.

Examples:

```text
1.00 verified careers link from official website
0.99 direct ATS link from verified employer page
0.95 ATS tenant metadata matches verified domain
0.75 probable match based on search evidence
0.45 ambiguous
```

Low confidence:

```text
COMPANY_SOURCE_REVIEW_REQUIRED
```

---


# 79. DISCOVERY PROVENANCE

Every learned source should be explainable.

Store evidence such as:

```text
Discovered from:
HiringCafe job 123

Company:
Acme

Official domain:
acme.com

Career URL:
...

ATS:
Greenhouse

Confidence:
0.98
```

---


# 80. COMPANY DISCOVERY SECURITY

External job content is untrusted.

Protect backend discovery from SSRF.

Validate:

```text
scheme
hostname
redirects
private IP ranges
loopback addresses
link-local addresses
file://
unexpected ports
```

Do not blindly fetch arbitrary internal addresses supplied by job descriptions.

---


# 81. COMPANY SOURCE UI

Provide a Companies/Sources view containing:

```text
Company
Official Domain
Careers URL
ATS
Source Type
Confidence
Priority
Status
Jobs Found
Relevant Jobs
Last Checked
Next Check
```

Actions:

```text
Run Now
Rediscover
Verify
Edit
Pin
Disable
View Evidence
```

---


# 82. COMPANY DISCOVERY ACTIVITY

Show useful events:

```text
Discovered Acme from Remote Rocketship
Resolved official domain
Detected Lever
Added direct employer source
Imported 47 active openings
6 match current profiles
```

Do not make users inspect raw debug logs for normal operation.

---


# 83. COMPANY SOURCE QUALITY SCORE

Calculate source quality using:

```text
freshness
completeness
availability
parser reliability
salary completeness
application-link accuracy
historical uptime
```

Use this when choosing authoritative values among duplicate postings.

---


# 84. COMPANY DISCOVERY SHOULD SHARE ATS KNOWLEDGE WITH AUTO-APPLY

If discovery already established:

```text
Acme → Greenhouse
```

the application subsystem should immediately know which adapter to use.

Do not rediscover known ATS information unnecessarily.

---
