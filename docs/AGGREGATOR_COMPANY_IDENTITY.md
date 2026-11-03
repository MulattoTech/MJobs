# Aggregator company identity conflicts

The collector refuses a source batch when one aggregator company identity has
conflicting employer labels. Some adapters derive identities from bounded ASCII
slugs; punctuation removal, non-ASCII names and truncation can otherwise map
unrelated labels to the same identity.

`SOURCE_COMPANY_IDENTITY_CONFLICT` is the fixed diagnostic. The collector checks
both labels within the incoming batch and the existing persisted company name.
The persisted check happens after the shared company-ingestion lock, including
when concurrent PostgreSQL collectors initially see no company row. A refused
batch rolls back company, source-link, job, route, posting, snapshot and candidate
changes. Existing source-health/backoff handling records the safe error without
including employer names or raw provider content in the diagnostic.

Comparison normalizes Unicode to NFC, collapses whitespace and ignores letter
case. It preserves punctuation and distinct non-ASCII characters. Equivalent
formatting remains compatible; the stored company name is retained. Existing
first-party Greenhouse/Lever name updates and verified company bindings keep
their established behavior.

This guard does not establish real-world employer identity. Different employers
with identical labels, including repeated `Unknown Company` labels, still need
stronger identity evidence. A genuine rebrand that conflicts with the stored
label also stops collection pending source/identity review. No automatic repair,
company merge, source-ID change or historical migration is performed.

## Verification

`tests/integration/test_aggregator_company_identity.py` exercises actual WWR,
Remote OK and Remotive adapters through synthetic HTTP responses, both collision
orders, reopened storage, rollback, equivalent formatting and ATS compatibility.

`tests/integration/test_aggregator_company_identity_postgres.py` uses an explicitly
disposable loopback database and a unique schema. Two real collectors contend on
the company advisory lock; exactly one company/job association is committed.

This is a scoped source-integrity contribution to MJ-REQ-017 / issue #18. Broader
source coverage remains PARTIAL. No live feed polling, paid model call, external
application submission or production-data repair is required for these tests.
