# Discovery services

Own the normalized discovery boundary for controlled fixtures and registered
public sources. The source contract declares collection strategy and exact
capabilities; provider-specific request behavior stays inside adapters.

Current production registration is Greenhouse public read-only. Source policy
persists enablement, independent cadence, concurrency, timeout, rate limit,
jitter, health and backoff. Search profiles are data-driven and persistent.
The scheduler performs no external I/O: it only creates idempotent durable
DISCOVER operations when an enabled source is due. Only the discovery worker
has the source-egress network.

The persisted per-source concurrency value remains policy metadata today; the
Greenhouse collector makes bounded sequential requests and does not spawn
per-source parallel fetches. Durable discovery operations instead use the
global `source_collection` admission ceiling documented in `workers/AREA.md`.

Matching can rescore multiple career profiles from one collected batch without
re-fetching the provider. Manual collection remains available for an immediate
bounded run and obeys the same source policy.

The manual job-entry service stores user-entered posting text, provenance and
application/reference links through the canonical job, posting snapshot,
application-route and audit records. It performs no network access and makes
no claim that user-entered employer/source identity is verified.

Requirements: MJ-REQ-016, 018, 019, 020, 021, 022 plus the existing mock
flywheel/dedupe acceptance requirements MJ-REQ-040, 041, 044, 052, 054, 063,
066, 067, 112, 114, 212.

Tests:
- python -m unittest tests.integration.test_greenhouse
- python -m unittest tests.integration.test_source_policy_scheduler
- python -m unittest tests.unit.test_compose_modes

External pages/HTML remain untrusted data. No CAPTCHA, authentication, access
control, anti-bot, proxy-identity or rate-limit circumvention belongs here.

## Canonical job lifecycle (MJ-REQ-085, 086, 087)

`lifecycle.py` records append-only source observations, resolves canonical status
using independently verified origin authority, and appends material description
revisions. `record_candidate` captures lifecycle evidence; `resolve_job_values`
updates canonical values, revisions and status in the same transaction. The
fictional fixture pipeline also captures material revisions. Both job list and
detail expose status; detail additionally exposes ordered evidence and revisions.

States are ACTIVE, POSSIBLY_ACTIVE, REMOVED, EXPIRED, FILLED, UNKNOWN and CLOSED.
Generic closure never invents a filled/expired reason. Exact structured feed
status claims are retained as source claims; feed presence alone is only
POSSIBLY_ACTIVE. A later ambiguous claim cannot clear a closure; explicit ACTIVE
from that source can reopen it. Authority is frozen with evidence at observation,
never taken from a feed field. Only already attached postings compete for one job;
this module does not infer cross-provider duplicate identity.

Only a successfully parsed complete native inventory establishes source removal.
Greenhouse metadata reporting a different total, parser rejections, failed reads,
and rolling RSS/JSON feed omissions never do. No job, raw snapshot, application,
source candidate, revision or lifecycle observation is deleted on disappearance.

Material revisions compare whitespace-normalized description/location, salary
bounds/currency/period and remote type/regions. They preserve original description
text and its SHA-256, nullable unknown location and salary, observation time (when
known), local recording time, and ordered sequence. Reversion creates a new
revision; refresh timestamps and status-only changes do not. Legacy canonical
content is captured on the next resolution; historical revisions before this
feature are not reconstructed or fabricated. Downgrade refuses populated history.

Tests: `python -m unittest tests.integration.test_job_lifecycle tests.integration.test_job_lifecycle_migration`.
