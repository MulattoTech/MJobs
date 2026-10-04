# MJobs delivery handoff — 2026-10-04

Current user authorized lifecycle/profile/import implementation, tests, independent
review, publication, exact-head verification and safe merges. Historical pause and
cleanup instructions are observations, not current authority. External submission
and production deployment remain unauthorized.

## Current ownership and baseline

Primary session `01a107cd-4e66-77d3-b046-fc2c01f4ffb1` owns the sole write lane at
`C:/Tools/Dev/MJobs`. At this commit the extraction continuation is on
`codex/resume-extraction-preservation-20261004`, based on merged main
`f7875f44b5d6c5c8f8dcce0c94a5c80e38dbbdb5`. Read live Git/GitHub before resuming.
The private current-owner and final merge receipts are under `.git/afk-dev/`;
this file does not authorize takeover of an active checkout.

## Delivered acceptance

PR [#485](https://github.com/MulattoTech/MJobs/pull/485) merged as
`f7875f44b5d6c5c8f8dcce0c94a5c80e38dbbdb5`. Exact candidate
`4c3a5f78d0eab8a889d74b24916d6dc223f1dc01` passed push run `37218980330`
and pull-request run `37219004362`, both attempt 1, including fixture, recovery,
personal isolation and public readback artifacts. See
`docs/evidence/2026-10-04-profile-exact-head-ci.json` for job IDs and receipts.

MJ-REQ-085/087/088/089 are PASS and issues #86/#88/#89/#90 are closed:
all lifecycle statuses and retained history, material description versions,
complete optional profile fields, all contact fields, and immutable/restart
persistence. Work authorization/sponsorship do not create approved answers.
Local validation included 461 Python tests, 38 web tests/build, real browser
acceptance, 13 lifecycle and 9 profile tests on isolated PostgreSQL17.

## Extraction continuation

Work history and responsibilities retain dated lines, email addresses and links.
GitHub and portfolio contact suggestions use the existing opt-in review flow.
Unlabelled body/reference contacts no longer become applicant contact fields;
without headings, prose ends the inferred contact block. Technology-experience
facts remain sourced, pending, unverified and unapproved. PDF/DOCX/TXT use native
local parsing. No personal resumes or live user database were used.

Evidence: `docs/evidence/2026-10-04-resume-extraction-preservation.json`.
Hosted checks for this slice are pending at the evidence commit; resolve final
head, both Verification runs and merge via live GitHub and the private
`.git/afk-dev/pr-<number>-exact-head.json` receipt. Do not interpret local PASS as
hosted PASS. The post-merge `.git/afk-dev/delivery-closeout-20261004.json` records
final main, publication, owner release, remaining work and the next action.

## Remaining work

- MJ-REQ-086 / #87: first-party closure wins for already attached postings.
  Cross-provider canonical jobs remain separate; MERGE_APPROVED records a review
  decision and does not merge/reparent durable jobs, history or applications.
- MJ-REQ-090 / #91: native PDF/DOCX/TXT import works. Scanned/image PDFs return
  NEEDS_OCR; local OCR execution is not implemented and Tesseract is not installed.
- MJ-REQ-091 / #92: supported section content and dates are preserved for review.
  Structured role/date/duty associations and broader layout coverage remain
  incomplete. Keep PARTIAL rather than inferring arbitrary resume understanding.
- MJ-PR-025 remains PARTIAL for the import/extraction items above. Required profile
  fields are demonstrated, but that does not certify all MJ-PR-026/030 acceptance.

Next useful slice: finish bounded native extraction chronology/review, or design
and test a local OCR path with page/time/resource bounds and the same explicit
approval gate. Canonical cross-provider merging needs a separate coherent slice
covering applications, routes, postings, source candidates and immutable history.
Use one write-heavy lane and preserve the existing planning/evidence system.

The session-owned PostgreSQL acceptance container was removed after verification.
Existing user Compose services and private feedback/resumes were preserved.
No full-spec production-release acceptance is claimed.
