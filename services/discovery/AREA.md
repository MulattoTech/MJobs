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

Matching can rescore multiple career profiles from one collected batch without
re-fetching the provider. Manual collection remains available for an immediate
bounded run and obeys the same source policy.

Requirements: MJ-REQ-016, 018, 019, 020, 021, 022 plus the existing mock
flywheel/dedupe acceptance requirements MJ-REQ-040, 041, 044, 052, 054, 063,
066, 067, 112, 114, 212.

Tests:
- python -m unittest tests.integration.test_greenhouse
- python -m unittest tests.integration.test_source_policy_scheduler
- python -m unittest tests.unit.test_compose_modes

External pages/HTML remain untrusted data. No CAPTCHA, authentication, access
control, anti-bot, proxy-identity or rate-limit circumvention belongs here.
