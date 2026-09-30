# MJobs Area Specification — job_identity_dedupe

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-040, MJ-REQ-041, MJ-REQ-042, MJ-REQ-043, MJ-REQ-044, MJ-REQ-045, MJ-REQ-046, MJ-REQ-047, MJ-REQ-048, MJ-REQ-049, MJ-REQ-050, MJ-REQ-051, MJ-REQ-052, MJ-REQ-053

---

# 40. JOB NORMALIZATION

All jobs must enter one normalized representation.

Suggested fields:

```text
canonical_job_id
title
normalized_title
title_family

company_id
company_name
normalized_company_name

description_html
description_text

responsibilities
required_qualifications
preferred_qualifications

skills

employment_type
experience_level

salary_min
salary_max
salary_currency
salary_period
salary_source

location_text
city
state
country
remote_type
remote_regions

reported_posted_at
first_seen_at
last_seen_at
expires_at

status
```

---


# 41. RAW SOURCE DATA

Never throw away useful source information.

Maintain a separate raw-source representation.

At minimum store:

```text
source
source_job_id
fetch_timestamp
raw structured payload
source URL
application URL
```

Optionally retain bounded HTML snapshots where useful for debugging.

---


# 42. CANONICAL JOB VS JOB POSTING

This distinction is mandatory.

Example:

```text
CanonicalJob

Acme Corporation
Junior Systems Administrator
Remote US

    ├── JobPosting: Dice
    ├── JobPosting: HiringCafe
    ├── JobPosting: Acme Careers
    └── JobPosting: Greenhouse
```

The default Jobs UI shows:

```text
ONE JOB
```

not four duplicate cards.

---


# 43. APPLICATION ROUTE IS ALSO SEPARATE

A listing URL and an application destination are not necessarily the same.

Model:

```text
JobPosting
       ↓
ApplicationRoute
```

Four source listings may all ultimately route to:

```text
Greenhouse requisition 123456
```

This must not result in four accidental duplicate submissions.

---


# 44. DEDUPLICATION — STRONG SIGNALS

Use strong identifiers first:

```text
ATS requisition ID
employer requisition ID
source job ID relationship
identical application URL
canonical application URL
ATS tenant + requisition ID
```

---


# 45. DEDUPLICATION — COMPANY IDENTITY

Normalize company identity using:

```text
aliases
domains
ATS metadata
official company identity
known parent/subsidiary relationships
```

Example:

```text
Acme
Acme Inc.
Acme, Inc
Acme Corporation
```

may refer to one company.

Do not merge unrelated organizations with similar names.

---


# 46. DEDUPLICATION — TITLE

Normalize:

```text
case
punctuation
spacing
common abbreviations
```

while retaining semantic differences.

---


# 47. DEDUPLICATION — DESCRIPTION SIMILARITY

Use appropriate combinations of:

```text
normalized hashes
TF-IDF
MinHash
SimHash
trigram similarity
embeddings
```

Do not require expensive embeddings for obvious exact duplicates.

---


# 48. COMPANY BOILERPLATE HANDLING

Descriptions frequently contain long identical employer boilerplate.

Similarity should reduce the influence of text such as:

```text
company culture
benefits boilerplate
EEO statement
standard legal footer
```

so two completely different jobs at the same employer are not incorrectly merged.

---


# 49. DEDUPLICATION CONFIDENCE

Calculate an explainable confidence.

Example:

```text
1.00 exact ATS requisition
0.97 same application route
0.94 same company/title + near-identical description
0.87 same company/title/location + high text similarity
0.65 possible duplicate
```

Thresholds must be configurable.

---


# 50. AMBIGUOUS DUPLICATES

Do not force uncertain merges.

Create:

```text
DUPLICATE_REVIEW_REQUIRED
```

where appropriate.

Allow users to:

```text
Merge
Keep Separate
Never Merge These
```

---


# 51. FIRST-PARTY SOURCE PRECEDENCE

If duplicate listings disagree, prioritize a verified first-party employer or ATS record.

For example:

```text
Aggregator:
Remote

Employer:
Hybrid – Dallas
```

Prefer the employer's value as normalized current state.

Preserve the aggregator value for provenance.

---


# 52. DO NOT DELETE DUPLICATE SOURCE RECORDS

Deduplication must preserve every source record.

The user may want to inspect:

```text
where job was discovered
how quickly each source discovered it
which source redirected where
which source produced an application
```

---


# 53. APPLY THROUGH MULTIPLE SOURCES

Support the user's ability to request:

```text
Apply through this source
Apply through employer site
Apply through all genuinely distinct routes
```

But before doing so determine whether the destinations are actually distinct.

Do not submit repeatedly to the same underlying requisition.

---
