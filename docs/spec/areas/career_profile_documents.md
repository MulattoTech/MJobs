# MJobs Area Specification — career_profile_documents

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-088, MJ-REQ-089, MJ-REQ-090, MJ-REQ-091, MJ-REQ-092, MJ-REQ-093, MJ-REQ-094, MJ-REQ-095, MJ-REQ-096, MJ-REQ-097, MJ-REQ-098, MJ-REQ-099, MJ-REQ-100, MJ-REQ-101, MJ-REQ-102, MJ-REQ-103, MJ-REQ-104, MJ-REQ-105, MJ-REQ-106, MJ-REQ-107

---

# 88. USER CAREER PROFILE

Create a normalized career profile distinct from any one resume.

Include:

```text
contact information
desired roles
location
remote preferences
employment preferences
salary preferences
work authorization
sponsorship needs
travel preference

work history
education
certifications
skills
projects
accomplishments
technology experience
career goals
```

---


# 89. USER CONTACT INFORMATION

Support:

```text
first name
middle name
last name
preferred name
email
phone
city
state
postal code
country
LinkedIn
GitHub
portfolio
```

Only collect fields actually useful.

---


# 90. BASE RESUME IMPORT

Support:

```text
PDF
DOCX
TXT
```

Use native extraction first.

OCR only for image/scanned resumes.

---


# 91. RESUME EXTRACTION

Extract:

```text
contact information
summary
experience
dates
responsibilities
achievements
skills
education
certifications
projects
```

Show extracted data for review.

Never silently treat uncertain extraction as verified truth.

---


# 92. MASTER CAREER FACT STORE

Create an explicit source-of-truth structure for verified career facts.

Conceptually:

```text
CareerFact

type
value
source
verified
confidence
user_approved
```

Generated application materials must ground themselves in approved facts.

---


# 93. DOCUMENT LIBRARY

Store:

```text
Base Resume
Master Resume
Tailored Resume
Cover Letter
Application Answer Export
Supporting Document
Certification
```

---


# 94. DOCUMENT VERSIONING

Every derivative document should have versions.

Example:

```text
base-resume-v1
acme-sysadmin-v1
acme-sysadmin-v2
```

Record:

```text
job
application
generation model
template
timestamp
facts used
```

---


# 95. RESUME TAILORING ENGINE

For a target job, the system may:

```text
adjust summary
reorder skills
highlight relevant technologies
choose relevant bullets
rephrase factual bullets
remove low-value unrelated detail
improve keyword alignment
```

---


# 96. RESUME FACTUAL VALIDATION

After generation:

compare assertions against verified career facts.

Flag:

```text
UNSUPPORTED_CLAIM
```

where necessary.

Do not automatically use a document containing unresolved unsupported claims.

---


# 97. ATS-FRIENDLY RESUMES

Default templates should avoid:

```text
skill bars
progress charts
decorative icons
complex multi-column layouts
images
text embedded in graphics
```

Use clean typography and conventional headings.

---


# 98. RESUME OUTPUTS

Generate:

```text
DOCX
PDF
plain text
```

where practical.

DOCX and PDF content should be consistent.

---


# 99. RESUME DIFF

Provide:

```text
Base Resume ↔ Tailored Resume
```

Highlight:

```text
summary changes
bullet changes
reordering
skills emphasized
content omitted
```

---


# 100. COVER LETTER ENGINE

Generate job-specific cover letters using:

```text
verified company
position
responsibilities
required skills
career facts
relevant accomplishments
verified company context
```

Do not hallucinate company details.

---


# 101. COVER LETTER LENGTH

Support:

```text
Very Short
Concise
Normal
Detailed
```

---


# 102. COVER LETTER STYLE

Allow user preference.

Examples:

```text
professional
warm
technical
direct
conversational
formal
```

Do not hard-code excessive enthusiasm.

---


# 103. ANSWER BANK

Create reusable application answers.

Fields might include:

```text
question pattern
normalized question
answer
tags
allowed_for_auto_use
requires_review
last_used
usage_count
verified
```

---


# 104. ANSWER MATCHING

Use:

```text
exact normalization
fuzzy matching
semantic similarity
```

to match variants such as:

```text
Are you authorized to work in the US?
Are you legally authorized for employment in the United States?
```

---


# 105. APPLICATION ANSWER CONFIDENCE

Example:

```text
Exact approved answer          1.00
Normalized approved match      0.99
Strong semantic match          0.95
Generated factual response     0.85
Unknown                        0.30
```

Use configurable thresholds.

---


# 106. SENSITIVE QUESTIONS

Never infer answers about:

```text
race
ethnicity
religion
sex
gender identity
disability
veteran status
other voluntary demographics
```

Only use explicit user-configured policy.

Supported policy:

```text
Prefer not to answer
Use configured response
Always require review
```

---


# 107. LEGALLY SIGNIFICANT QUESTIONS

Treat questions regarding:

```text
work authorization
sponsorship
criminal-history attestations
minimum-age attestations
conflicts of interest
legal certifications
salary expectations
non-compete obligations
```

as explicit profile/policy values.

Never invent an answer.

---
