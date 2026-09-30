# Durable authority

Own relational profiles/fact versions, companies/sources, jobs/postings/routes,
matches/documents, applications/attempts, operations and audits. Interfaces:
Store(url), save_profile, profile(version), jobs, application, transition.
Requirements: MJ-REQ-015, 041, 052, 092, 094, 145, 146, 156, 158, 159, 162.
SQLAlchemy Core; PostgreSQL deployment, explicit SQLite test URLs only. Migrations
live in migrations/versions and must retain immutable schema history. UI and
provider services cannot become durable state authority. Test: .venv/Scripts/python
-m unittest discover -s tests. Credentials and personal artifacts are never logged
or committed. Counters and submission reservations serialize by profile row.
