# Career profile extraction

This area implements the native, local-first base-resume import boundary for MJ-REQ-090,
MJ-REQ-091, and MJ-REQ-092. PDF, DOCX, and UTF-8 TXT input is parsed without network or
AI calls. Results remain `READY_FOR_REVIEW` until a user approves individual facts; empty
image-only PDFs use bounded local OCR in the Linux Docker runtime. If OCR is
unavailable or unsuccessful they report `NEEDS_OCR`; blank/unreadable documents
report `UNREADABLE`. Native text remains reviewable even when OCR fails.

Public interface: `extract_resume(data, format, source)` returns bounded contact
suggestions, pending facts, and extraction warnings. It never writes profile data,
fetches remote links, invokes AI, or approves facts. Source text is untrusted data.
The profile API checks uploaded-file ownership and checksum before extraction;
explicit profile saves use immutable versions owned by persistence. Extraction
must not depend on application automation or matching workers.

Native PDF parsing uses pypdf; DOCX parses only the bounded document XML part;
both run in a shell-free child process with a 15-second parent wall deadline,
streamed JSON output capped at 2 MiB, client-disconnect cancellation, discarded stderr,
and a small environment allowlist
that excludes application credentials. Linux parser children also receive address
space, CPU, file-size and core-dump limits; Windows relies on the parent wall deadline
and streaming output cap. Request disconnect cancels and reaps both native parser and
currently running OCR tool children. The profile UI also has an explicit, profile- and
resume-scoped cancellation action for browsers that keep an HTTP connection reusable
after a fetch abort. OCR retains its own 45-second total deadline and 10-second
per-tool deadline for non-cancelled requests.
UTF-8 TXT stays local in the API process. Scanned pages without native text use Poppler and English
Tesseract locally; no file or extracted text goes to a remote OCR service.
The fallback processes at most 10 scanned pages, scales each raster to at most
1800 pixels per dimension, and has a 45-second total deadline with 10-second
tool deadlines. Linux child processes have 768 MiB address-space, 12-second CPU,
16 MiB file-size limits and one OCR thread. A per-process slot prevents parallel
OCR work. Each child captures stderr only into a temporary file bounded by the
child file-size limit. At most 16 KiB of diagnostic-size metadata is retained;
diagnostic contents are never returned, logged, or persisted because they may
contain private paths or document data. Timeouts, launch failures, nonzero exits,
missing output, and output-limit failures receive distinct sanitized review
warnings. Private temporary files are removed on every outcome. Hosts without
the supported tools/resource limits retain the explicit unavailable state.
Mixed PDFs retain page order and native text. OCR results carry a warning and
confidence 70, remain unverified/unapproved, and need deliberate review before
document use. OCR can misread even clear text; never silently correct guesses.
Inline-image-only pages and pages mixing native text with scanned image content
are not promised complete OCR coverage. Rich structured chronology remains open.
Native extraction recognizes work/employment history, responsibilities and
technology-experience headings and preserves dated lines verbatim. Only whole
contact tokens in the opening header, or explicit labelled contact lines, become
contact suggestions; email addresses and links in career prose remain facts.
GitHub and explicit portfolio/website links use the existing opt-in contact review.
Responsibilities have their own pending career-fact type. An optional `date_ranges`
review projection links a single unambiguous year/month range to its original
fact ID. Original text and fact IDs are unchanged; missing days, tenure and
role/duty associations are never inferred. Reversed or ambiguous dates remain
plain text. Date projections are shown during review; the original dated fact,
not a separate structured chronology record, is saved in the profile.
Without headings, prose ends the inferred header. No links are fetched and no
imported fact is automatically verified or approved.
Tests: `python -m unittest tests.unit.test_resume_extraction
tests.integration.test_profile_import tests.browser.test_profile_import`.
OCR bounds/failure tests: `python -m unittest tests.unit.test_resume_ocr`.
Real tool/API/browser acceptance in the app image: `python -m cli.profile_ocr_e2e`.

MJ-REQ-088/089 use the existing immutable profile snapshots for optional contact
and job preferences. Work authorization is user-supplied text; sponsorship is
NOT_SPECIFIED, REQUIRED or NOT_REQUIRED. Neither becomes an approved application
answer. Technology experience is a sourced career-fact kind with the same explicit
verification/approval gate as other facts. No new tables or migration are needed.

Profile job preferences also own an optional bounded `avoid_terms` list. Terms are
user-configured, case-folded and deduplicated, and versioned with the immutable
profile snapshot. The web editor exposes one term per line. They are not career
facts or reusable answers and are not copied into generated document metadata.

`build_profile_context` in `profile_access.py` is the least-data boundary for
future AI task integrations. It accepts only registered task names and dotted
paths within each task's fixed allowlist; optional deny paths can only narrow
that grant. Only verified, user-approved facts and answers can be returned.
Container requests produce bounded metadata previews, never all child values.
Accepted operations are appended to `audit_events` with the task, operation ID,
profile version, paths and accessed fact/answer IDs, never values. Denials record
the fixed task scope, operation ID, profile version and reason, with no requested
path or value.
Fact/answer values are capped at 4,000 characters each and the entire projected
context at 32,000 characters; oversize inputs fail closed without truncation.
There is no AI provider or provider-execution path in the current repository.
The provider-neutral `services.agents.task_prompts.build_task_prompt` builder
consumes this projection rather than a raw profile; future task prompt
construction must use the same boundary. The projection and prompt contract are
exercised locally with SQLite and synthetic profiles; they make no provider
calls.
