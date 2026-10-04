# Current delivery handoff — 2026-10-04

Sole writer: root session `01a107cd-4e66-77d3-b046-fc2c01f4ffb1`, branch
`codex/resume-date-review-20261004`, based on main
`e8f8ba6c46e83b976c9b565549c0f6f0367415d0`.

PR485/486/488 are merged with exact-head push and PR CI. Tracked receipts:
`docs/evidence/2026-10-04-profile-exact-head-ci.json`,
`docs/evidence/2026-10-04-extraction-exact-head-ci.json`, and
`docs/evidence/2026-10-04-lifecycle-exact-head-ci.json`.
MJ-PR024 acceptance is complete (issues86/87/88 closed); profile/contact
issues89/90 also closed. Canonical attachment preserves existing records;
legacy duplicate reparent/merge remains separate dedupe work.

Current MJ-REQ091 slice adds conservative date-range review projections and
explicit responsibility facts using the existing review/editor/persistence
path. Original text and pending approval flags are preserved. Focused30 tests,
38 frontend tests/build, built-browser acceptance and independent review passed.
Full local regression479 tests passed;31 affected tests passed after final
date-boundary hardening. Final-head hosted push/PR gates remain required before
merge. See `docs/evidence/2026-10-04-resume-date-review.json`; final receipts go
in the PR closeout and `.git/afk-dev/pr-<number>-exact-head.json` until the next
tracked slice. Do not close issue92 before those gates and merge.

Next: finish MJ-REQ090 / issue91 with a bounded local OCR fallback for scanned
resumes. Native PDF/DOCX/TXT parsing already exists. The current response for
image-only PDFs is NEEDS_OCR. Preserve original/private uploads, prefer native
text, bound pages/pixels/time, retain unverified review and never send resumes
to a remote OCR service. Structured career role/duty associations are not inferred.

Private source files and user Compose services are preserved. Session-owned
PostgreSQL acceptance containers were removed after identity verification.
No external application submission or production deployment is authorized.
