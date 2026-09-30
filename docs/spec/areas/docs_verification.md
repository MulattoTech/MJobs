# MJobs Area Specification — docs_verification

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-281, MJ-REQ-282, MJ-REQ-283, MJ-REQ-284, MJ-REQ-285, MJ-REQ-286, MJ-REQ-287, MJ-REQ-288, MJ-REQ-289

---

# 281. REPOSITORY STRUCTURE

Choose a clear structure.

Example only:

```text
apps/
  web/
  api/

workers/
  discovery/
  company_discovery/
  applications/

packages/
  shared/

tests/
docs/
docker/
```

Use the repository's existing conventions if better.

---


# 282. SOURCE ADAPTER DEVELOPMENT GUIDE

Create:

```text
docs/ADDING_JOB_SOURCE.md
```

Document:

```text
adapter interface
normalization
rate limiting
health
fixtures
tests
```

---


# 283. COMPANY SOURCE DEVELOPMENT GUIDE

Create:

```text
docs/ADDING_COMPANY_SOURCE.md
```

Explain:

```text
company resolution
career discovery
ATS detection
tenant extraction
job enumeration
```

---


# 284. APPLICATION ADAPTER DEVELOPMENT GUIDE

Create:

```text
docs/ADDING_APPLICATION_ADAPTER.md
```

Explain:

```text
ATS detection
navigation
field mapping
uploads
questions
validation
submission
confirmation
tests
```

---


# 285. ARCHITECTURE DOCUMENT

Create:

```text
docs/ARCHITECTURE.md
```

Include:

```text
services
database
workers
queues
source pipeline
company discovery
dedupe
matching
documents
application automation
recovery
security
```

Use Mermaid diagrams where useful.

---


# 286. DATABASE DOCUMENTATION

Include an ER diagram or equivalent.

---


# 287. TROUBLESHOOTING GUIDE

Document:

```text
source broke
company discovery failed
ATS changed
browser won't launch
login expired
CAPTCHA
resume extraction failed
AI unavailable
database unavailable
queue unavailable
worker stalled
```

---


# 288. VERIFICATION UTILITY

Create a verification command producing something conceptually like:

```text
Job Platform Verification

[PASS] Configuration valid
[PASS] Database schema current
[PASS] PostgreSQL reachable
[PASS] Redis reachable
[PASS] Scheduler healthy
[PASS] Discovery worker healthy
[PASS] Company discovery worker healthy
[PASS] Browser worker healthy
[PASS] Document engine healthy
[PASS] Dry-run mode functional

Sources:
[PASS] adapter registry
[PASS] mock global source
[PASS] mock company discovery
[PASS] ATS detector

Application:
[PASS] mock application fill
[PASS] mock upload
[PASS] mock confirmation
[PASS] duplicate-submission protection
```

Never fabricate a PASS. Code presence, mocks alone, assumptions, or unexecuted commands are not PASS evidence.

---


# 289. DEEP VERIFICATION

Provide a separately invoked deeper integration test.

It may:

```text
run mock discovery
perform company discovery
generate test documents
launch Playwright
submit mock application
verify confirmation
verify persistence
```

Do not make this run every application startup.

---
