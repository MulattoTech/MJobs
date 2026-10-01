"""Relational authority for the initial product spine."""
from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, JSON, MetaData, String, Table, UniqueConstraint, false

metadata = MetaData()


def table(name, *columns, constraints=()):
    return Table(name, metadata, Column("id", String(128), primary_key=True), *columns, *constraints)


workspace_identity = table("workspace_identity", Column("operation_mode", String(16), nullable=False),
                           Column("bound_at", String(64), nullable=False))


profiles = table("career_profiles", Column("name", String(200), nullable=False),
                 Column("email", String(320), nullable=False), Column("version", String(64), nullable=False))
profile_versions = table("profile_versions", Column("profile_id", ForeignKey("career_profiles.id"), nullable=False),
                         Column("version", String(64), nullable=False), Column("contact", JSON, nullable=False), Column("answers", JSON, nullable=False))
facts = table("career_facts", Column("profile_id", ForeignKey("career_profiles.id"), nullable=False),
              Column("version", String(64), nullable=False), Column("fact_id", String(128), nullable=False),
              Column("kind", String(32), nullable=False), Column("text", String(10000), nullable=False),
              Column("source", String(1000), nullable=False), Column("verified", Boolean, nullable=False),
              Column("user_approved", Boolean, nullable=False), Column("confidence", Float, nullable=False))
uploaded_resumes = table("uploaded_resumes", Column("profile_id", ForeignKey("career_profiles.id"), nullable=False),
                         Column("filename", String(200), nullable=False), Column("format", String(8), nullable=False),
                         Column("media_type", String(128), nullable=False), Column("size_bytes", Integer, nullable=False),
                         Column("sha256", String(64), nullable=False), Column("status", String(32), nullable=False),
                         Column("created_at", String(64), nullable=False),
                         constraints=(UniqueConstraint("profile_id", "sha256"),))
companies = table("companies", Column("name", String(200), nullable=False), Column("domain", String(1000), nullable=False))
company_sources = table("company_job_sources", Column("company_id", ForeignKey("companies.id"), nullable=False),
                        Column("provider", String(32), nullable=False), Column("tenant", String(128), nullable=False),
                        Column("endpoint", String(2000), nullable=False), Column("discovered_automatically", Boolean, nullable=False))
jobs = table("canonical_jobs", Column("company_id", ForeignKey("companies.id"), nullable=False),
             Column("requisition", String(128), nullable=False), Column("title", String(500), nullable=False),
             Column("description", String(50000), nullable=False), Column("skills", JSON, nullable=False),
             Column("location", String(1000), nullable=False), Column("remote_type", String(32), nullable=False),
             Column("attributes", JSON, nullable=False, server_default="{}"))
routes = table("application_routes", Column("job_id", ForeignKey("canonical_jobs.id"), nullable=False),
               Column("company_id", ForeignKey("companies.id"), nullable=False), Column("provider", String(32), nullable=False),
               Column("tenant", String(128), nullable=False), Column("requisition", String(128), nullable=False),
               Column("url", String(2000), nullable=False),
               constraints=(UniqueConstraint("company_id", "provider", "tenant", "requisition"),))
postings = table("job_postings", Column("job_id", ForeignKey("canonical_jobs.id"), nullable=False),
                 Column("source", String(200), nullable=False), Column("source_job_id", String(128), nullable=False),
                 Column("source_url", String(2000), nullable=False), Column("application_url", String(2000), nullable=False),
                 Column("raw", JSON, nullable=False), Column("fetched_at", String(64), nullable=False),
                 constraints=(UniqueConstraint("source", "source_job_id"),))
posting_snapshots = table("posting_snapshots", Column("posting_id", ForeignKey("job_postings.id"), nullable=False),
                          Column("raw", JSON, nullable=False), Column("sha256", String(64), nullable=False),
                          Column("retrieved_at", String(64), nullable=False))
dedupe_reviews = table("dedupe_reviews", Column("left_job_id", ForeignKey("canonical_jobs.id"), nullable=False),
                       Column("right_job_id", ForeignKey("canonical_jobs.id"), nullable=False),
                       Column("confidence", Float, nullable=False), Column("state", String(64), nullable=False),
                       Column("evidence", JSON, nullable=False), Column("decision", String(32)),
                       Column("created_at", String(64), nullable=False), Column("updated_at", String(64), nullable=False),
                       constraints=(UniqueConstraint("left_job_id", "right_job_id"),))
job_sources = table("job_sources", Column("provider", String(32), nullable=False), Column("tenant", String(128), nullable=False),
                    Column("name", String(200), nullable=False), Column("enabled", Boolean, nullable=False),
                    Column("strategy", String(32), nullable=False), Column("capabilities", JSON, nullable=False),
                    Column("poll_interval", Integer, nullable=False), Column("concurrency", Integer, nullable=False),
                    Column("request_timeout", Integer, nullable=False), Column("rate_limit", Integer, nullable=False),
                    Column("jitter_seconds", Integer, nullable=False),
                    Column("last_attempt", String(64)), Column("last_success", String(64)), Column("backoff_until", String(64)),
                    Column("consecutive_failures", Integer, nullable=False, server_default="0"),
                    Column("health_status", String(64), nullable=False), Column("last_error", String(200)),
                    Column("jobs_seen", Integer, nullable=False, server_default="0"))
search_profiles = table("search_profiles", Column("profile_id", ForeignKey("career_profiles.id"), nullable=False),
                        Column("name", String(200), nullable=False), Column("enabled", Boolean, nullable=False),
                        Column("criteria", JSON, nullable=False), Column("source_ids", JSON, nullable=False),
                        Column("created_at", String(64), nullable=False), Column("updated_at", String(64), nullable=False),
                        constraints=(UniqueConstraint("profile_id", "name"),))
matches = table("job_matches", Column("job_id", ForeignKey("canonical_jobs.id"), nullable=False),
                Column("profile_id", ForeignKey("career_profiles.id"), nullable=False), Column("profile_version", String(64), nullable=False),
                Column("score", Float, nullable=False), Column("missing", JSON, nullable=False), Column("components", JSON, nullable=False))
applications = table("applications", Column("job_id", ForeignKey("canonical_jobs.id"), nullable=False),
                     Column("route_id", ForeignKey("application_routes.id"), nullable=False),
                     Column("profile_id", ForeignKey("career_profiles.id"), nullable=False), Column("profile_version", String(64), nullable=False),
                     Column("state", String(64), nullable=False), Column("target", String(32), nullable=False),
                     Column("fact_ids", JSON, nullable=False), Column("trace", JSON, nullable=False),
                     Column("document_version", Integer, nullable=False, server_default="1"),
                     Column("confirmation", JSON), Column("created_at", String(64), nullable=False), Column("submitted_at", String(64)),
                     constraints=(UniqueConstraint("job_id", "profile_id"),))
application_route_choices = table("application_route_choices",
                                  Column("application_id", ForeignKey("applications.id"), nullable=False),
                                  Column("route_id", ForeignKey("application_routes.id"), nullable=False),
                                  Column("underlying_key", String(2200), nullable=False),
                                  Column("requested_mode", String(32), nullable=False),
                                  Column("state", String(32), nullable=False),
                                  Column("created_at", String(64), nullable=False),
                                  constraints=(UniqueConstraint("application_id", "underlying_key"),))
documents = table("documents", Column("application_id", ForeignKey("applications.id"), nullable=False),
                  Column("kind", String(32), nullable=False), Column("version", Integer, nullable=False),
                  Column("profile_version", String(64), nullable=False),
                  Column("files", JSON, nullable=False), Column("fact_ids", JSON, nullable=False),
                  Column("sha256", JSON, nullable=False), Column("template", String(128), nullable=False))
attempts = table("application_attempts", Column("application_id", ForeignKey("applications.id"), nullable=False),
                 Column("state", String(64), nullable=False), Column("steps", JSON, nullable=False), Column("started_at", String(64), nullable=False))
operations = table("worker_operations", Column("kind", String(64), nullable=False), Column("entity_id", String(128), nullable=False),
                   Column("params", JSON, nullable=False), Column("state", String(64), nullable=False),
                   Column("error", String(1000)), Column("updated_at", String(64), nullable=False),
                   Column("attempt_count", Integer, nullable=False, server_default="0"),
                   Column("cancel_requested", Boolean, nullable=False, server_default=false()),
                   Column("lease_expires_at", String(64)))
audit = table("audit_events", Column("entity_id", String(128), nullable=False), Column("action", String(64), nullable=False),
              Column("payload", JSON, nullable=False), Column("timestamp", String(64), nullable=False))
