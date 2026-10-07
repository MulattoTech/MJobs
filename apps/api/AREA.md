# Product API

Own local profile input, durable discovery/operation routes, job reads and explicit
mock application preparation/run/status. FastAPI/Pydantic interfaces are in OpenAPI
at /docs. Requirements: MJ-REQ-181 and initial spine cross-area integration.
No career fact inference, workflow truth in UI, or external submission switch.
Personal operation rejects fictional discovery, preparation and mock-run before
queueing; `/settings` reports the active mode. Public source collection remains
available. Queued fixture work is also blocked by workers after a mode switch.
Production startup refuses PERSONAL mode if the database contains mock routes.
`POST /jobs/manual` persists explicitly entered job text and links through the
canonical job, posting snapshot, application route and audit records. It never
fetches the supplied URLs; retries require the same request key and content,
while a new key creates a distinct record.
`GET/PUT /profiles/{profile_id}/ai-task-settings` reads and persists non-secret
provider/model choices per profile. Invalid selections fail closed without
echoing submitted credentials or malformed values.
`GET/PUT /profiles/{profile_id}/ai-cost-policy` persists bounded per-profile
funnel thresholds. `GET /profiles/{profile_id}/ai-cost-plan` is a read-only
preview over stored deterministic match scores; it makes no provider request
and never creates documents. Validation errors omit submitted values.
`POST /profiles/{profile_id}/ai-cost-plan/evaluate` requires the configured
job-scoring provider and runs only after an explicit request. Validated advisory
results, the applied cost policy, and non-secret provider/model identifiers are
durably stored against the profile version used; profile changes during
inference reject persistence. `GET /profiles/{profile_id}/ai-cost-plan/evaluations`
returns bounded recent history for that profile. Neither route changes match
scores or creates documents.
Depends on domain/services/persistence; browser work belongs in workers. Contact
and facts are bounded and strict; cross-origin mutations are rejected; default
deployment publishes only localhost. Test: python -m unittest
tests.integration.test_api_queue. Full user authentication, authorization,
dashboard/API coverage and production exposure remain unimplemented.
