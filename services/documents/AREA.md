# Factual documents

Own verified-fact DOCX/PDF/TXT preparation and version/provenance/hash records.
Also own profile-scoped base-resume storage. PDF, DOCX and UTF-8 TXT uploads
are bounded to 10 MiB, validated before storage, marked STORED_UNREVIEWED,
and checksum-checked on download. Uploads never create or approve CareerFacts.
Private originals live in the document volume's uploads directory, so the
offline database/document backup must preserve metadata and bytes together.
Interface: prepare(store, job_id, profile_id, output_dir) -> application_id.
Requirements: MJ-REQ-092 through 099; MJ095 remains partial.
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
qualification. Explicit approved fact selection/omission and matched skill/technology
emphasis are supported. MJ-REQ095 remains PARTIAL: broader summary adjustment and
factual rephrasing are not implemented. Tests: unit/integration/browser
.test_skill_order, plus hosted cli.ui_e2e explicit opt-in through the worker.

Factual validation (MJ-REQ096) compares generated paragraphs with the exact
approved fact IDs/text from the immutable profile, plus fixed identity/template
fields. Unknown/changed assertions fail as UNSUPPORTED_CLAIM. Serialized DOCX,
TXT and PDF are read back before any new revision is committed; PDF comparison
preserves factual word boundaries and allows physical wraps only inside exact
expected HTTP(S) URL tokens; every URL character must remain unchanged. DOCUMENTS_VALIDATED audit receipts bind the
validator, profile version, fact IDs and all file hashes. The local mock adapter
revalidates the current resume against its original profile before browser use,
including historical rows without a receipt. Rejection becomes REVIEW_REQUIRED;
explicit preparation repairs it with a new revision, retaining prior bytes.

MJ-REF-010 adds a claim-preservation ledger to each validated generated revision
and its DOCUMENTS_VALIDATED audit receipt. The ledger records typed fact IDs,
protected field categories, and date/numeric token counts; it does not copy claim
text, token values, or guessable token hashes into logs. The immutable document
profile version and fact IDs bind the result to its saved source. Experience/education/certification fields are
protected as their complete approved source sentence, so employer, institution,
degree, and credential wording cannot be independently rewritten. Novel date or
numeric claims are rejected before staging publication, while exact comparison
blocks other unapproved edits. Keep/drop authority remains deterministic template
and explicit user fact selection; no AI relevance scorer or provider is called.
Entity tokenization beyond dates/numbers is not inferred: preserving the complete
typed fact sentence is the lossless boundary. Tests: unit/integration.test_document_validation,
tests.integration.test_spine, and tests.browser.test_document_history.

MJ-REF-003 adds deterministic Unicode-normalized whole-term avoidance to profile
preferences. Resume, cover-letter and reviewed answer generation blocks matching
content before publishing a revision or export; the safe error names only the
configured term and output path, never surrounding content. Current preferences
also gate downloads, document history and local mock-adapter use of older
revisions, so adding a term cannot leave prior conflicting output usable. The
avoid list is consumed only by local post-generation validation and is not copied
into prompt/document contexts or audit payloads (the local path uses no AI prompt).
Tests cover possessives,
partial-word false positives, Unicode, profile isolation, history preservation,
answer exports and browser-visible block reasons. No new database migration.

PDF output embeds Windows Arial or Linux DejaVu Sans (installed in the app
image). Font registration and rendering are serialized because ReportLab font
objects cannot be shared by simultaneous documents. Missing fonts, unsupported
glyphs/bidi direction and readback mismatches fail explicitly; this is not a
universal Unicode/shaping promise. No output silently substitutes characters.
Tests: unit/integration.test_document_validation; the integration cases also run
inside the actual app image in required hosted Verification. No new migration.

Generated revision comparison reads two explicit resume IDs under one application:
a known generic base and a known skill-tailored output. It checks TXT path bounds,
size, hash and exact immutable approved-fact/profile content before projecting
summary changes, bullet/fact changes, shared-fact reordering, recorded skill
matches, omissions/additions and contact changes. Fact identity, not text matching,
anchors the diff; duplicate/Unicode text and live-profile edits do not change it.
Missing snapshots/context or corrupt/unknown outputs produce an unavailable
response. The Applications and library history UI lets users choose revisions and
see the actual stored text while keeping downloads. No records or files mutate.
Uploaded original bases are also compared using bounded native extraction/offline
OCR, exact occurrence alignment, preserved original hashes and explicit uncertainty.
See uploaded_comparison.py and its real OCR runtime acceptance. MJ099 is accepted.
Tests: unit/integration.test_resume_comparison, browser.test_skill_order and the
PostgreSQL-backed hosted cli.ui_e2e comparison flow. No migration.

Explicit include_resume_contact=true adds saved phones, city/state/country and
LinkedIn/GitHub/portfolio links to the resume only. It defaults false and is carried
through the API and durable worker with the captured profile version. Full street
addresses and postal codes are excluded. Contact base/tailored/selected template
pairs preserve historical bytes and opt-out behavior. Rendering, assertion checks,
comparison and emphasis indexing share the same versioned header helper; no contact
line is classified as a career fact. History lists the included snapshot values.
Tests: integration.test_resume_contact_header, unit.test_pdf_url_fidelity and the
connected browser/hosted cli.ui_e2e contact opt-in and prior-revision checks.
