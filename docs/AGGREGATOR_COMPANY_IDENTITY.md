# Aggregator company identity conflicts

The collector refuses a source batch when one WWR, Remote OK or Remotive
company identity has conflicting employer labels. These adapters derive
identities from bounded ASCII slugs; punctuation removal, non-ASCII names and
truncation can otherwise map unrelated labels to the same identity.

`SOURCE_COMPANY_IDENTITY_CONFLICT` is the fixed conflict diagnostic. The
collector compares full labels within the incoming batch, then checks a
retained source-label receipt under the shared company-ingestion lock. This
also serializes concurrent PostgreSQL collectors that initially see no company
row. The receipt is scoped to the provider and company, across configured
tenants. It stores a versioned canonical-label SHA-256 and bounded evidence
references in the existing audit table; it does not store a rejected label.

The company display name and domain are preserved independently. A preexisting
company without source history may acquire its first receipt only when its stored
name is equivalent to the incoming full label. A different stored name requires
identity review; the lossy ID alone does not establish an authorized alias.
Validated source history or an existing receipt remains authoritative if stored
display fields later change. This change does not add a company-renaming API.
First-party Greenhouse/Lever bindings and extension adapters keep their established
behavior.

A refused batch rolls back company, source-link, job, route, posting, snapshot,
candidate and newly proposed receipt changes. Existing source-health/backoff
handling records a fixed error code without including employer names or raw
provider content in the diagnostic. The receipt is inserted only as part of a
successful collection transaction; ordinary subsequent polls use its primary
key and do not rescan historical snapshots.

Comparison normalizes Unicode to NFC, collapses whitespace and ignores letter
case. It preserves punctuation and distinct non-ASCII characters. Equivalent
formatting remains compatible. The comparison uses full adapter labels, never
the normalized display attribute that is truncated to 200 characters.

## Existing data and review limits

A legacy identity without a receipt can recover its original labels from the
exact stored raw snapshots linked to prior source-value candidates. Recovery
checks provider, source and posting attribution, snapshot checksums and the
provider-derived company identity. Every recovered label must agree with the
incoming label. The existing curated display name is not used as identity proof.

Recovery examines at most 64 distinct snapshots, with one overflow sentinel.
Missing or malformed provenance, an invalid receipt, or more than 64 legacy
snapshots produces `SOURCE_COMPANY_IDENTITY_REVIEW_REQUIRED`. This is a
conservative availability limit: even a legitimate mature company may require
manual source/identity review. Repeated candidates for one snapshot do not
consume additional snapshot slots. The collector does not select an arbitrary
historical winner or automatically clear this review condition.

This guard does not establish real-world employer identity. Different employers
with identical labels, including repeated `Unknown Company` labels, still need
stronger identity evidence. A genuine rebrand that conflicts with the retained
label also stops collection pending review. No automatic company merge,
source-ID change, schema migration or historical repair is performed. Receipt
and history integrity follow the existing application's append-only conventions;
this change does not add database-level tamper resistance.

## Verification

`tests/integration/test_aggregator_company_identity.py` exercises actual WWR,
Remote OK and Remotive adapters through synthetic HTTP responses, both collision
orders, reopened storage, rollback, equivalent formatting and ATS compatibility.
The history tests cover curated display names, exact legacy labels, ambiguous
or malformed provenance, bounded recovery and transactional receipt rollback.
The existing source-enrollment API contract must also continue to pass.

`tests/integration/test_aggregator_company_identity_postgres.py` uses an explicitly
disposable loopback database and a unique schema. Two real collectors contend on
the company advisory lock; exactly one company/job association is committed.

This is a scoped source-integrity contribution to MJ-REQ-017 / issue #18. Broader
source coverage remains PARTIAL. No live feed polling, paid model call, external
application submission or production-data repair is required for these tests.
