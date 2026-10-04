# Factual documents

Own verified-fact DOCX/PDF/TXT preparation and version/provenance/hash records.
Also own profile-scoped base-resume storage. PDF, DOCX and UTF-8 TXT uploads
are bounded to 10 MiB, validated before storage, marked STORED_UNREVIEWED,
and checksum-checked on download. Uploads never create or approve CareerFacts.
Private originals live in the document volume's uploads directory, so the
offline database/document backup must preserve metadata and bytes together.
Interface: prepare(store, job_id, profile_id, output_dir) -> application_id.
Requirements: MJ-REQ-092 through 097 (initial deterministic slice only).
Depend on domain safety/persistence, python-docx/reportlab. No AI or invented
qualifications. Private files reside under ignored outputs or the document
volume, scoped to application and immutable profile version. Test:
python -m unittest tests.integration.test_spine tests.integration.test_resume_upload.
Named base resumes retain immutable version entries in resume_revisions; repeated
uploads within a series are idempotent and version allocation locks the profile.
Repeated content keeps the first authoritative filename, media type, format, and
stored path even when a later request uses a different matching extension.
Native extraction and review belong to services/career_profile. Broader tailoring,
advanced structured chronology and document regeneration remain incomplete.
Bounded local scanned-PDF OCR and its review gates are owned by career_profile.

Generated revisions record their actual creation timestamp and deterministic
copy method, template, immutable profile version, approved fact IDs and SHA-256
for each available format. All three formats use the same paragraph sequence.
Application history resolves fact text/source against the original profile
snapshot and reports missing IDs rather than substituting current profile facts.
The UI exposes this provenance and only links formats present in that revision.
Legacy timestamps/methods remain unknown (NULL); migration af2049e17b63 refuses
downgrade that would discard populated provenance. Existing bytes are unchanged.
Tests: tests.integration.test_document_provenance and tests.browser.test_document_history.
The unified library supports seven uploaded types; broader tailoring remains incomplete.

Document library collections (MJ-REQ-093) classify uploaded originals as base,
master or tailored resume, cover letter, application answer export, supporting
document or certification. These labels are user declarations, not validation.
All accept bounded PDF/DOCX/UTF-8 TXT through the existing validated blob store.
Kinds live on immutable resume_revisions; identical bytes share the original
profile/content-addressed blob while non-base kinds use distinct series IDs.
A collection cannot change kind. Profile row locks serialize version allocation.
Legacy IDs/labels/versions remain BASE_RESUME via migration be3105f82a74; downgrade
refuses to discard non-base kinds. Resume list/download/extract require base
membership. Library downloads require a profile-owned revision ID plus checksum.
The profile library includes uploaded versions and generated application history;
generic generated resumes remain GENERATED_RESUME, not TAILORED_RESUME. Uploads
never create or approve facts. Private backup continues to preserve relational
metadata and the same uploads directory. Tests: test_document_library and the
real browser library flow in test_document_history; hosted cli.ui_e2e covers a
PostgreSQL-backed certification upload/download with no import/fact mutation.

Explicit tailor_resume=true orders only approved kind=skill facts within existing
skill slots using normalized target skill overlap and stable ties. It preserves
all text and every non-skill position, including role/date/responsibility lines;
cover letters remain generic. The API requires a strict boolean and the durable
worker defaults absent flags to false. Model/template/context distinguish this
bounded tailored output from generic generation. Context stores original/ordered
fact IDs, matched job skills and the original target job snapshot/fingerprint.
Mode/target/profile changes create new revisions only in safe preparation states;
identical requests are idempotent and confirmed/active history stays locked.
Migration cf4216a93b85 preserves legacy unknown context and refuses to discard
recorded tailoring context. Matching is a relevance heuristic, not proof of a
qualification. MJ-REQ095 remains PARTIAL: summary adjustment, rephrasing, and
bullet selection/removal are not implemented. Tests: unit/integration/browser
.test_skill_order, plus hosted cli.ui_e2e explicit opt-in through the worker.

Factual validation (MJ-REQ096) compares generated paragraphs with the exact
approved fact IDs/text from the immutable profile, plus fixed identity/template
fields. Unknown/changed assertions fail as UNSUPPORTED_CLAIM. Serialized DOCX,
TXT and PDF are read back before any new revision is committed; PDF comparison
normalizes layout whitespace only. DOCUMENTS_VALIDATED audit receipts bind the
validator, profile version, fact IDs and all file hashes. The local mock adapter
revalidates the current resume against its original profile before browser use,
including historical rows without a receipt. Rejection becomes REVIEW_REQUIRED;
explicit preparation repairs it with a new revision, retaining prior bytes.

PDF output embeds Windows Arial or Linux DejaVu Sans (installed in the app
image). Font registration and rendering are serialized because ReportLab font
objects cannot be shared by simultaneous documents. Missing fonts, unsupported
glyphs/bidi direction and readback mismatches fail explicitly; this is not a
universal Unicode/shaping promise. No output silently substitutes characters.
Tests: unit/integration.test_document_validation; the integration cases also run
inside the actual app image in required hosted Verification. No new migration.
