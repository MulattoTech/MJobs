# Factual documents

Own verified-fact DOCX/PDF preparation and version/provenance/hash records.
Interface: prepare(store, job_id, profile_id, output_dir) -> application_id.
Requirements: MJ-REQ-092 through 097 (initial deterministic slice only).
Depend on domain safety/persistence, python-docx/reportlab. No AI or invented
qualifications. Private files reside under ignored outputs or the document
volume, scoped to application and immutable profile version. Test:
python -m unittest tests.integration.test_spine. Resume import, broader tailoring,
document review UX and regeneration/recovery remain later queue acceptance.
