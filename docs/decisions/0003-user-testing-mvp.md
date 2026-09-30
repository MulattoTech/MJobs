# Connected local user-testing milestone

The user's active goal authorizes autonomous implementation through a verified
user-testing milestone. Preserve the complete requirement catalogue; this is
scoped delivery, not full-spec completion. The implemented worker branch is the
base. Activate MJ-PR-149 for one connected user-visible slice; keep four active
implementation PRs maximum and integrate in dependency order after required
checks. Git writes are now verified after the user's explicit Git/GitHub grant.
Remote CI remains blocked until native workflow OAuth consent succeeds.

Serve a strict TypeScript React/Vite build from the existing FastAPI process.
This preserves one localhost origin and one public port, avoids a disconnected
frontend shell and keeps startup to a project-local command/Compose. Production
uses PostgreSQL and the three durable RQ worker processes. No UI control enables
real external submission.

Provide profile setup/read/edit with explicit sourced facts and separate
verification/approval; jobs with search/sort/remote filtering and honest match
explanations; bounded read-only Greenhouse board configuration/collection;
factual document preparation and integrity-checked downloads; dry-run and
explicit fictional mock submission; application tracking, attempts and
operation/readiness diagnostics. Mock demo data is loaded only on explicit
request and visibly labeled fictional. Personal data remains in ignored local
database/document/browser volumes.

Greenhouse uses its documented unauthenticated public GET API over one fixed
HTTPS host. Tenant identifiers are validated; redirects, credentials and
arbitrary URLs are refused. Persist source policy/cooldown/backoff and raw
payload snapshots with checksums. Keep provider parsing within the adapter.
Each collection is bounded in bytes/jobs/time and a failed source does not
erase jobs or block other sources. Discovered application URLs are metadata;
the Greenhouse source has no submission capability.

Matching on mock structured required skills remains exact coverage. Live
description skill mentions are heuristic evidence and must be labeled as such,
without asserting they are mandatory requirements or suitability. Generate
documents only from verified approved facts; pending facts stay visible and
are excluded. Keep prepared profile snapshots immutable across later edits.

Acceptance: fresh local startup, connected browser journey, real public-source
read validation, factual downloads, dry-run and mock receipt once, persisted UI
readback after restart, visible failed/cancelled/blocked states and a concise
user-testing guide. General recurring scheduling, imports, AI, all providers,
advanced matching, analytics and manual challenge resume remain future work.
