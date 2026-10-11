# Evidence navigation

Use the compact `planning/REQUIREMENT_INDEX.json` and the relevant canonical requirement matrix before opening evidence files. Read only the evidence for the issue or failure being investigated.

Dated records describe the revision and environment actually tested, not necessarily current main. A passing subset never proves its whole parent requirement. The full historical directory is retained because existing requirements and provenance records link to it; deleting or moving those records without repairing references would destroy auditability.

New CI evidence belongs in short-retention workflow artifacts and sanitized summaries, not copied terminal transcripts. Applicant data, private credentials and local raw logs must not be committed.
