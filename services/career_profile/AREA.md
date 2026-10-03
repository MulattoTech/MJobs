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
Tests: `python -m unittest tests.unit.test_resume_extraction
tests.integration.test_profile_import tests.browser.test_profile_import`.
