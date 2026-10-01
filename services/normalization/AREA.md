# AREA — services/normalization

Owns deterministic canonical-job presentation and normalization boundaries.

Rules:
- Provider-specific raw payloads remain in job postings/snapshots.
- Canonical views may normalize representation but must not erase provenance.
- Company/title normalization is not permission to merge identities.
- Application routes remain independent from listing/posting URLs.
- Ambiguous cross-source deduplication belongs to the later dedupe PRs.
