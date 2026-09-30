# First vertical spine

CODEX_START section 7 and MJ-REQ-290 explicitly prioritize a verified vertical
slice over a waterfall. The original queue orders the test harness after most
of the UI and applications. Add MJ-PR-148 as the initial cross-area mock spine
without deleting, completing, or superseding the broader queue requirements.
Its base is the implemented governance branch, not an unimplemented dependency.
Main remains unchanged; PRs are stacked and must integrate in dependency order.

Use FastAPI, SQLAlchemy, PostgreSQL, Redis/RQ and headless Playwright. SQLite is
only an explicitly configured local unit/integration test backend. Mock services
are separate from product services, and the product labels this execution MOCK.
Real external application workers are not enabled by this milestone.

Persist raw postings, company sources, canonical route identity, profiles/facts,
matches, documents, applications/attempts and operations independently. Dispatch
persisted QUEUED operations through RQ with deterministic identifiers; queue
failure cannot erase the durable operation. Application identity is unique across
profile revisions for a canonical route. Persist SUBMITTING before a click;
an uncertain result reconciles a receipt or becomes SUBMISSION_UNCONFIRMED.

The initial supported mock ATS performs a real two-page headless browser flow,
including document uploads and confirmation. The adapter restricts all browser
requests to its configured local mock origin. Source data and HTML remain
untrusted data. Factual documents contain only verified approved profile facts.
Read-only production adapters and UI are subsequent coherent slices.
