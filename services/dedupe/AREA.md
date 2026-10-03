# AREA — services/dedupe

Owns deterministic duplicate-candidate evidence and later merge/review policy.

Rules:
- Strong identifiers outrank fuzzy text.
- Name similarity alone never establishes company identity.
- Preserve every posting and raw source record.
- Candidate evidence is not permission to auto-merge ambiguous records.
- Confidence/review thresholds are owned by later dedupe PRs.

## Canonical source values

`source_values.py` records immutable normalized candidates tied to raw snapshots and successful observations. Only postings already attached to one canonical job participate. Verified authority precedes coverage-adjusted quality; the selected bundle retains its own salary, location and requirement qualifiers. Raw authority flags are not proof. Selection never rewrites application-used or explicitly chosen route targets. Changed targets require explicit preserved-route review in both the API and preparation worker. Unknown metrics remain unknown; link verification is a separate incomplete requirement.
