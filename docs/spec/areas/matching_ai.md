# MJobs Area Specification — matching_ai

This file is a **context partition** generated from the original MJobs master directive.
The requirement text below is preserved verbatim from the source so Codex can load one
area at a time instead of the entire 8,246-line specification.

Authority: `AGENTS.md` + the active GitHub issue/PR + this area spec.
For ambiguity only, consult `docs/spec/source/MJobs_Startup_Prompt.original.md`.

Covered requirements: MJ-REQ-108, MJ-REQ-109, MJ-REQ-110, MJ-REQ-111, MJ-REQ-112, MJ-REQ-113, MJ-REQ-114, MJ-REQ-115, MJ-REQ-116, MJ-REQ-117, MJ-REQ-118, MJ-REQ-119, MJ-REQ-120

---

# 108. AI PROVIDER ABSTRACTION

Do not couple the application to one model.

Support an abstraction such as:

```text
AIProvider
```

Possible implementations:

```text
OpenAI-compatible
OpenAI
Azure OpenAI
Ollama
local endpoint
disabled
```

---


# 109. AI TASK CONFIGURATION

Configure different models for:

```text
job parsing
job scoring
requirement extraction
resume tailoring
cover letters
free-form answers
company summarization
```

---


# 110. AI COST CONTROLS

Do not run expensive AI over every job immediately.

Use a funnel.

Example:

```text
5,000 discovered
   ↓
dedupe
   ↓
deterministic filters
   ↓
800 plausible
   ↓
deterministic score
   ↓
200 strong candidates
   ↓
AI evaluate 75
   ↓
documents generated for 20
```

Make thresholds configurable.

---


# 111. AI STRUCTURED OUTPUT

Prefer schema-validated output.

Use:

```text
Pydantic
JSON Schema
typed result objects
```

Do not trust arbitrary free-form JSON.

---


# 112. JOB MATCHING ENGINE

Every job receives a transparent score.

Example:

```text
91/100 — Excellent Match
```

---


# 113. MATCH COMPONENTS

Score dimensions may include:

```text
title similarity
required skills
preferred skills
experience
education
certifications
remote/location
salary
employment type
industry
company preference
negative requirements
```

---


# 114. EXPLAINABLE MATCHING

Example:

```text
+20 System Administrator title match
+12 Active Directory
+10 Windows Server
+8 PowerShell
+7 Microsoft 365
+6 VMware
+6 Remote US
+5 salary range
+5 relevant infrastructure experience

-5 bachelor's preferred
-3 Azure certification preferred

Score: 91
```

---


# 115. REQUIRED VS PREFERRED

Requirement extraction must distinguish:

```text
REQUIRED
PREFERRED
NICE_TO_HAVE
UNKNOWN
```

Missing a preferred technology should not penalize the score as severely as missing a mandatory qualification.

---


# 116. MATCH CATEGORIES

Suggested display:

```text
95–100 Exceptional
85–94 Excellent
75–84 Strong
60–74 Possible
40–59 Weak
0–39 Poor
```

Exact thresholds may be tuned.

---


# 117. MATCH WEIGHT CONFIGURATION

Allow advanced configuration.

Example:

```text
title               20
skills              25
experience          15
location            15
salary              10
education            5
company              5
industry             5
```

Do not hard-code this as the only model.

---


# 118. APPLICATION PRIORITY

Match score and priority are not identical.

Priority may consider:

```text
match
posting age
salary
company preference
direct employer availability
application complexity
source freshness
expiration risk
```

---


# 119. FRESH JOB PRIORITY

New excellent matches should be processed before stale lower-quality jobs.

Use queue priorities.

---


# 120. FEEDBACK

Allow user feedback:

```text
Good match
Bad match
Too senior
Wrong technology
Wrong location
Bad salary
Bad company
Not interested
```

Use it to improve deterministic preferences.

Do not silently change hard application rules.

---
