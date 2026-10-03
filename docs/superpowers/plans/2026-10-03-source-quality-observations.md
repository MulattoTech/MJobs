# MJ-REQ-083 source quality observations

Live starting point: main `0ab211153f3f9da7669b1832b554aa996cef8942`, no open PRs.
MJ-PR-023 dependencies (012 and 022) are merged. MJ-REQ-083 remains incomplete.

Implement a durable collection-evidence vertical slice before using quality to
change authoritative job values. Add append-only observations, explicit configured
collector/company-source linkage, seven explainable quality components, and API
projections. Successful imports and failed attempts must survive restart; failed
imports must not leave partial jobs. Do not infer historical observations from
last-success metadata. Do not probe arbitrary application URLs or equate their
syntax with accuracy. Unmeasured components remain null with explicit coverage.

Scoring uses at most 200 observations in the preceding 30 days, equal component
weights, explicit reference time and sample counts. Freshness means collection
freshness. Historical uptime means observed attempt availability, never wall-clock
uptime. Scores confer neither verified employer identity nor submission permission.

Validation: observe failing integration/unit tests, migrate existing SQLite and
isolated PostgreSQL data, collect valid/partial/failed/empty batches, reopen stores,
validate API source attribution and missing evidence, run affected/full regression
and existing live-feedback checks. Record exact commands and outcomes before PR.

Follow-up within MJ-REQ-083: safe application-link identity checks; normalized
posting candidate provenance; authority-first, quality-second duplicate selection
without merging uncertain identities. This slice alone must not promote MJ-REQ-083
to PASS. External submission remains disabled.
