# Career profile extraction

This area implements the native, local-first base-resume import boundary for MJ-REQ-090,
MJ-REQ-091, and MJ-REQ-092. PDF, DOCX, and UTF-8 TXT input is parsed without network or
AI calls. Results remain `READY_FOR_REVIEW` until a user approves individual facts; empty
image-only PDFs report `NEEDS_OCR` and ordinary parse failures report `UNREADABLE`.

Public interface: `extract_resume(data, format, source)` returns bounded contact
suggestions, pending facts, and extraction warnings. It never writes profile data,
fetches remote links, invokes AI, or approves facts. Source text is untrusted data.
The profile API checks uploaded-file ownership and checksum before extraction;
explicit profile saves use immutable versions owned by persistence. Extraction
must not depend on application automation or matching workers.

Native PDF parsing uses pypdf; DOCX parses only the bounded document XML part;
UTF-8 TXT stays local. OCR and rich structured chronology remain incomplete.
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

MJ-REQ-088/089 use the existing immutable profile snapshots for optional contact
and job preferences. Work authorization is user-supplied text; sponsorship is
NOT_SPECIFIED, REQUIRED or NOT_REQUIRED. Neither becomes an approved application
answer. Technology experience is a sourced career-fact kind with the same explicit
verification/approval gate as other facts. No new tables or migration are needed.
