# AREA — services/dedupe

Owns deterministic duplicate-candidate evidence and later merge/review policy.

Rules:
- Strong identifiers outrank fuzzy text.
- Name similarity alone never establishes company identity.
- Preserve every posting and raw source record.
- Candidate evidence is not permission to auto-merge ambiguous records.
- Confidence/review thresholds are owned by later dedupe PRs.
