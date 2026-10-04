# Current delivery handoff — 2026-10-04

Sole active writer: root session `01a107cd-4e66-77d3-b046-fc2c01f4ffb1`,
branch `codex/canonical-route-lifecycle-20261004`, based on main
`77b98129384f440d3769f4ca21ca6073c517f577` (includes independent Codespaces PR487).
Historical branch/session fields are observations, not authorization.

PR485 and PR486 are merged with exact-head push and PR CI receipts in
`docs/evidence/2026-10-04-profile-exact-head-ci.json` and
`docs/evidence/2026-10-04-extraction-exact-head-ci.json`.
Issues86/88/89/90 are closed from acceptance evidence.

Current slice implements MJ-REQ086 through both real ingestion paths: exact
same-company specific application URLs attach new postings before authority
resolution. Native-ID contradictions, generic URLs and existing duplicate jobs
remain separate. Raw source evidence, job/route/application IDs and history stay
preserved. Local476-test regression,38 frontend tests/build,22 PostgreSQL tests
and independent review passed. See
`docs/evidence/2026-10-04-canonical-route-lifecycle.json`.
Final content-head push/PR workflows remain the merge gate; exact receipt belongs
to the PR closeout and private `.git/afk-dev/pr-<number>-exact-head.json` until the
following slice records it. Do not close issue87 before that gate and merge.

Continue with MJ-PR025: issues91/92 remain PARTIAL. Native PDF/DOCX/TXT extraction,
contact review, preserved employment lines and responsibilities already exist.
Next useful increment is conservative employment date/duty associations in the
existing explicit review flow. Image-only PDFs currently report NEEDS_OCR;
local bounded OCR remains unimplemented. Do not infer full automatic extraction
or promote unreviewed career facts.

User Compose services/private feedback/resumes are preserved. The isolated
PostgreSQL acceptance container is session-owned and must be identity-checked
before removal. No production release or external submission is authorized.
