# User-testing MVP execution plan

Spec: docs/decisions/0003-user-testing-mvp.md. The user's active /goal supplies
implementation authorization; continue without repeated design-approval pauses.

1. Add bounded first-party source contract and Greenhouse adapter, source policy
   persistence, immutable posting snapshots and conservative matching. Run red/
   green fixture tests, migration and bounded live public GET proof.
2. Extend API read models for profiles, jobs/match provenance, applications,
   documents and operations. Add safe integrity-checked download endpoints,
   selected-fact preparation and failure/cancellation/reconciliation views.
3. Build connected React/TypeScript UI with explicit fictional demo, editable
   approved facts, jobs/details, source configuration, document downloads,
   dry-run/mock application buttons and durable tracker/diagnostics. Add targeted
   component validation and real browser E2E.
4. Add project-local startup/testing commands, compile UI in Docker, execute
   fresh-start, deployed E2E, live read-only source and restart readback tests.
5. Perform independent review, resolve material findings, record precise
   evidence/limits, update state/queue/capability matrix and publish a fourth
   coherent draft. Required remote CI/integration remain distinct gates.
