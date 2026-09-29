# MJobs Area Specification — web_api_analytics

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-181, MJ-REQ-182, MJ-REQ-183, MJ-REQ-184, MJ-REQ-185, MJ-REQ-186, MJ-REQ-187, MJ-REQ-188, MJ-REQ-189, MJ-REQ-190, MJ-REQ-191, MJ-REQ-192, MJ-REQ-193, MJ-REQ-194, MJ-REQ-195, MJ-REQ-196, MJ-REQ-197, MJ-REQ-198, MJ-REQ-199, MJ-REQ-200, MJ-REQ-201, MJ-REQ-202, MJ-REQ-203, MJ-REQ-204, MJ-REQ-205, MJ-REQ-206, MJ-REQ-207

---

# 181. SOURCE HEALTH DASHBOARD

Display:

```text
Source
Type
Strategy
Health
Last Success
Last Failure
Jobs Seen
New Jobs
Duplicates
Duration
Next Run
```

Include:

```text
Global Sources
Company Sources
```

---


# 182. COMPANY HEALTH DASHBOARD

For each direct employer source:

```text
Company
ATS
Priority
Jobs
Relevant Jobs
Last Validation
Last Collection
Consecutive Failures
```

---


# 183. WORKER HEALTH

Display:

```text
worker heartbeat
running tasks
queue depth
failed tasks
average duration
```

Detect stale workers.

---


# 184. ERROR MANAGEMENT UI

Provide a diagnostics page containing:

```text
source failures
company discovery failures
ATS detection failures
browser failures
AI errors
document failures
submission failures
```

Actions may include:

```text
Retry
Dismiss
Inspect
Open Job
Open Application
Rediscover Company
```

---


# 185. PRIMARY UI NAVIGATION

A reasonable layout:

```text
Dashboard
Jobs
Saved Searches
Companies
Applications
Review Queue
Documents
Automation
Sources
Analytics
Settings
Diagnostics
```

---


# 186. JOBS SCREEN

Provide:

```text
search
sort
filters
job cards
job detail pane
```

---


# 187. JOB CARD

Display:

```text
Title
Company
Location
Remote
Salary
Posted Age
Match Score
Source Count
ATS
Application Capability
Application Status
```

Useful badges:

```text
NEW
REMOTE
EXCELLENT MATCH
DIRECT EMPLOYER
3 SOURCES
AUTO-APPLY
REVIEW REQUIRED
APPLIED
```

---


# 188. JOB DETAIL

Show:

```text
description
requirements
skills
salary
location
remote restrictions
match explanation
company
all source postings
application routes
generated documents
application history
notes
```

---


# 189. JOB FILTER SIDEBAR

Support:

```text
keywords
title
company
location
remote
salary
experience
date
employment type
skills
education
clearance
match score
source
ATS
application capability
application state
```

---


# 190. JOB SORTING

Support:

```text
Best Match
Newest
Application Priority
Salary
Company
Title
```

---


# 191. SAVED SEARCHES

Users can save current filters.

Each saved search may have its own scheduler.

---


# 192. JOB ACTIONS

Provide:

```text
Save
Hide
Not Interested
Blacklist Company
Generate Resume
Generate Cover Letter
Prepare Application
Apply
Apply Direct
Apply via Selected Route
Apply via Distinct Routes
Open Original
Add Note
```

---


# 193. BULK ACTIONS

Support safe bulk:

```text
Save
Hide
Generate Documents
Queue Applications
Change Status
```

Bulk auto-submit must require explicit user policy.

---


# 194. COMPANIES SCREEN

Display:

```text
Company
Domain
Career Site
ATS
Source Priority
Relevant Jobs
Applications
User Preference
```

---


# 195. APPLICATION TRACKER

Provide a pipeline/Kanban view.

Example:

```text
Saved
Ready
Applied
Recruiter Contact
Interview
Technical Interview
Final Interview
Offer
Rejected
Withdrawn
```

---


# 196. REVIEW QUEUE

Centralize:

```text
CAPTCHA
login
unknown question
sensitive question
low-confidence field
unsupported ATS
ambiguous submission
document validation
```

Each review item must explain exactly what is needed.

---


# 197. APPLICATION REVIEW UX

Example:

```text
Acme — System Administrator

Reason:
Unknown mandatory question

Question:
Describe your experience managing Azure subscriptions.

Suggested answer:
...

Confidence:
82%

[ Edit ]
[ Save to Answer Bank ]
[ Continue ]
```

---


# 198. DOCUMENTS SCREEN

Allow:

```text
view
download
compare
edit
regenerate
archive
```

for resumes and cover letters.

---


# 199. AUTOMATION SCREEN

Show:

```text
global automation state
queue depth
pending applications
source schedule
company discovery schedule
daily submission count
limits
rules
```

---


# 200. DASHBOARD METRICS

Display:

```text
Jobs discovered today
Unique jobs
Duplicates merged
New companies
Direct company sources added
Strong matches
Applications prepared
Applications submitted
Manual reviews
Interviews
Offers
```

---


# 201. ANALYTICS

Provide useful metrics:

```text
jobs over time
applications over time
source effectiveness
company-source effectiveness
average match
interview rate
rejection rate
salary distribution
ATS success rate
manual-intervention rate
```

Do not let analytics delay core functionality.

---


# 202. SOURCE ATTRIBUTION ANALYTICS

Track:

```text
Where was the job first discovered?
Where was authoritative posting found?
Which source led to application?
Which source tends to discover jobs fastest?
```

---


# 203. FIRST-PARTY FRESHNESS METRIC

Track:

```text
first_seen_anywhere
first_seen_first_party
first_seen_by_source
```

This allows evaluation of whether direct employer monitoring improves discovery speed.

---


# 204. NOTIFICATIONS

Implement in-app notifications.

Abstract future providers:

```text
email
ntfy
Discord
Slack
desktop
```

Potential events:

```text
Excellent match
Application submitted
Application failed
Review required
Authentication expired
Source degraded
New employer source discovered
```

---


# 205. API DESIGN

Expose intention-oriented APIs.

Example:

```text
GET /jobs
GET /jobs/{id}
POST /jobs/{id}/save
POST /jobs/{id}/generate-resume
POST /jobs/{id}/prepare-application

GET /companies
POST /companies/{id}/discover
POST /companies/{id}/rediscover

GET /applications
POST /applications/{id}/resume
POST /applications/{id}/retry

GET /sources
POST /sources/{id}/run

GET /operations/{id}
```

Exact routes may differ.

---


# 206. REAL-TIME UI UPDATES

Use:

```text
SSE
WebSockets
```

where useful.

Examples:

```text
Searching HiringCafe
84 postings found
57 duplicates
27 new
Resolving companies
Scoring matches
Application ready for review
```

Do not poll every page aggressively if real-time events are cleaner.

---


# 207. CLI

Provide useful maintenance commands.

Conceptual examples:

```bash
jobapp health
jobapp source list
jobapp source run hiringcafe
jobapp search run <profile>
jobapp company discover <id>
jobapp company rediscover <id>
jobapp ats detect <url>
jobapp dedupe run
jobapp application retry <id>
jobapp backup
```

Exact syntax may differ.

---
