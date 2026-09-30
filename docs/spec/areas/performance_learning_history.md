# MJobs Area Specification — performance_learning_history

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-231, MJ-REQ-232, MJ-REQ-233, MJ-REQ-234, MJ-REQ-235, MJ-REQ-236, MJ-REQ-237, MJ-REQ-238, MJ-REQ-239, MJ-REQ-240, MJ-REQ-241, MJ-REQ-242, MJ-REQ-243, MJ-REQ-244, MJ-REQ-245, MJ-REQ-246, MJ-REQ-247

---

# 231. PERFORMANCE TARGET

The application should comfortably handle:

```text
hundreds of thousands of historical postings
tens of thousands of canonical jobs
thousands of company sources
thousands of applications over time
```

without loading entire datasets into browser memory.

---


# 232. SERVER-SIDE PAGINATION

Use server-side pagination for jobs.

Do not fetch 100,000 jobs into the browser.

---


# 233. RATE LIMITING

Each external source needs bounded request behavior.

Avoid accidental request storms.

---


# 234. CENTRAL HTTP CLIENT

Use a shared HTTP layer supporting:

```text
timeouts
connection pooling
retry
rate limiting
user agent
logging
caching where appropriate
```

Do not duplicate inconsistent networking logic across adapters.

---


# 235. BROWSER RESOURCE MANAGEMENT

Implement:

```text
page timeout
browser timeout
context recycling
process cleanup
maximum active browsers
```

---


# 236. SEARCH HISTORY

Store:

```text
source
query
profile
filters
start
finish
results
new jobs
duplicates
errors
```

---


# 237. COMPANY DISCOVERY HISTORY

Store:

```text
company
trigger
evidence
attempt
results
ATS
confidence
source created
errors
```

---


# 238. APPLICATION HISTORY

Never discard historical application state.

Maintain an event timeline.

---


# 239. USER NOTES

Support notes on:

```text
jobs
companies
applications
```

---


# 240. COMPANY BLACKLIST

Allow:

```text
blacklist
preferred
neutral
```

---


# 241. STAFFING / RECRUITER CLASSIFICATION

Attempt to classify:

```text
direct employer
staffing agency
recruiter
unknown
```

Do not automatically reject staffing firms unless configured.

---


# 242. SCAM / QUALITY SIGNALS

Add warning heuristics for:

```text
unrealistic salary
payment request
messaging-app-only interview
suspicious domain
unknown company identity
job text reused across unrelated employers
```

Display:

```text
Potential Risk
```

Do not make unsupported definitive accusations.

---


# 243. COMPANY SOURCE SEARCH FILTERING

A company with 20,000 jobs should not require AI analysis of all jobs.

Use inexpensive filters first.

Example:

```text
20,000 openings
↓ title/keyword filter
240 plausible
↓ deterministic normalization
80 relevant
↓ scoring
20 AI enrichment
```

---


# 244. SEARCH PROFILE SUGGESTIONS

The application may suggest related job titles discovered in strong matches.

Example:

```text
User searches:
System Administrator

Strong matches also use:
Infrastructure Support Engineer
IT Operations Engineer
Platform Administrator
```

Suggest them.

Do not silently expand auto-apply criteria.

---


# 245. AUTOMATED SOURCE LEARNING

Over time, source performance may inform scheduling.

Example:

```text
Company A posts frequently and produces excellent matches
→ poll more often

Company B has produced no relevant jobs for six months
→ poll less often
```

Respect minimum/maximum limits.

---


# 246. SOURCE FAILURE RECOVERY

After repeated failures:

```text
HEALTHY
↓
DEGRADED
↓
FAILED
```

For company sources, failure may trigger rediscovery.

---


# 247. DO NOT DELETE FAILED SOURCES

Preserve history and configuration.

A failed parser does not mean the employer disappeared.

---
