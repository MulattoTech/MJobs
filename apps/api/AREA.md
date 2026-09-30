# Product API

Own local profile input, durable discovery/operation routes, job reads and explicit
mock application preparation/run/status. FastAPI/Pydantic interfaces are in OpenAPI
at /docs. Requirements: MJ-REQ-181 and initial spine cross-area integration.
No career fact inference, workflow truth in UI, or external submission switch.
Personal operation rejects fictional discovery, preparation and mock-run before
queueing; `/settings` reports the active mode. Public source collection remains
available. Queued fixture work is also blocked by workers after a mode switch.
Production startup refuses PERSONAL mode if the database contains mock routes.
Depends on domain/services/persistence; browser work belongs in workers. Contact
and facts are bounded and strict; cross-origin mutations are rejected; default
deployment publishes only localhost. Test: python -m unittest
tests.integration.test_api_queue. Full user authentication, authorization,
dashboard/API coverage and production exposure remain unimplemented.
