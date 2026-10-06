# Durable authority

Own relational profiles/fact versions, companies/sources, jobs/postings/routes,
matches/documents, applications/attempts, operations and audits. Interfaces:
Store(url), save_profile, profile(version), ai_task_settings,
save_ai_task_settings, ai_cost_policy, save_ai_cost_policy, jobs, application,
transition.
Requirements: MJ-REQ-015, 041, 052, 092, 094, 145, 146, 156, 158, 159, 162.
SQLAlchemy Core; PostgreSQL deployment, explicit SQLite test URLs only. Migrations
live in migrations/versions and must retain immutable schema history. UI and
provider services cannot become durable state authority. Test: .venv/Scripts/python
-m unittest discover -s tests. Credentials and personal artifacts are never logged
or committed. Counters and submission reservations serialize by profile row.

Document generation metadata is additive and nullable only for legacy unknowns.
Never backfill a generation timestamp from application creation time.
Profile AI task settings store only provider/model identifiers, default to no
rows (all providers disabled), and are isolated by profile with cascade ownership.
AI cost thresholds are stored per profile; missing policy rows use bounded code
defaults. The persistence layer remains the durable authority for both settings.
