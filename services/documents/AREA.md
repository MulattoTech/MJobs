# Factual documents

Own verified-fact DOCX/PDF preparation and version/provenance/hash records.
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
Native resume extraction, reviewed fact import, broader tailoring, document
review UX and regeneration/recovery remain later queue acceptance.
