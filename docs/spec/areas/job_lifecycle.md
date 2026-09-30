# MJobs Area Specification — job_lifecycle

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-085, MJ-REQ-086, MJ-REQ-087

---

# 85. JOB STATUS

Persist statuses such as:

```text
ACTIVE
POSSIBLY_ACTIVE
REMOVED
EXPIRED
FILLED
UNKNOWN
```

Do not delete jobs when they disappear.

---


# 86. CLOSED JOB FIRST-PARTY AUTHORITY

If:

```text
Aggregator listing:
still online

Employer requisition:
closed
```

prefer:

```text
CLOSED
```

for the canonical status.

---


# 87. DESCRIPTION VERSIONING

Maintain job history when descriptions materially change.

Store:

```text
description hash
description
salary
location
remote policy
timestamp
```

---
