"""Relational authority for the initial product spine."""
from sqlalchemy import Boolean, CheckConstraint, Column, Float, ForeignKey, Index, Integer, JSON, MetaData, String, Table, UniqueConstraint, false, true

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
resume_revisions = table("resume_revisions", Column("profile_id", ForeignKey("career_profiles.id"), nullable=False),
                         Column("upload_id", ForeignKey("uploaded_resumes.id"), nullable=False),
                         Column("series_id", String(128), nullable=False), Column("label", String(200), nullable=False),
                         Column("version", Integer, nullable=False), Column("created_at", String(64), nullable=False),
                         constraints=(UniqueConstraint("profile_id", "series_id", "version"),
                                      UniqueConstraint("profile_id", "series_id", "upload_id")))
companies = table("companies", Column("name", String(200), nullable=False), Column("domain", String(1000), nullable=False),
                  Column("parent_company_id", ForeignKey("companies.id")),
                  Column("relationship_type", String(32)))
company_aliases = table("company_aliases", Column("company_id", ForeignKey("companies.id"), nullable=False),
                        Column("alias", String(200), nullable=False), Column("normalized_alias", String(200), nullable=False),
                        Column("alias_type", String(32), nullable=False), Column("confidence", Float, nullable=False),
                        Column("verified", Boolean, nullable=False), Column("source", String(200), nullable=False),
                        Column("evidence", JSON, nullable=False), Column("created_at", String(64), nullable=False),
                        constraints=(UniqueConstraint("company_id", "normalized_alias"),))
company_domains = table("company_domains", Column("company_id", ForeignKey("companies.id"), nullable=False),
                        Column("domain", String(253), nullable=False), Column("confidence", Float, nullable=False),
                        Column("verified", Boolean, nullable=False), Column("verification_method", String(64), nullable=False),
                        Column("first_seen_at", String(64), nullable=False), Column("last_verified_at", String(64)),
                        Column("evidence", JSON, nullable=False),
                        constraints=(UniqueConstraint("company_id", "domain"),))
company_ats_tenants = table(
    "company_ats_tenants", Column("company_id", ForeignKey("companies.id"), nullable=False),
    Column("provider", String(32), nullable=False), Column("tenant_identifier", String(128), nullable=False),
    Column("organization_identifier", String(128), nullable=False),
    Column("board_identifier", String(128), nullable=False), Column("confidence", Float, nullable=False),
    Column("evidence", JSON, nullable=False), Column("first_seen_at", String(64), nullable=False),
    Column("last_seen_at", String(64), nullable=False),
    constraints=(UniqueConstraint("company_id", "provider", "tenant_identifier",
                                  "organization_identifier", "board_identifier"),),
)
company_sources = table(
    "company_job_sources", Column("company_id", ForeignKey("companies.id"), nullable=False),
    Column("provider", String(32), nullable=False), Column("tenant", String(128), nullable=False),
    Column("endpoint", String(2000), nullable=False), Column("discovered_automatically", Boolean, nullable=False),
    Column("origin", String(32), nullable=False, server_default="AUTO_DISCOVERED"),
    Column("ats_tenant_id", ForeignKey("company_ats_tenants.id")),
    Column("career_url", String(2000)), Column("search_url", String(2000)),
    Column("feed_url", String(2000)), Column("public_api_url", String(2000)),
    Column("collection_strategy", String(32), nullable=False, server_default="UNKNOWN"),
    Column("confidence", Float, nullable=False, server_default="0.45"),
    Column("enabled", Boolean, nullable=False, server_default=true()),
    Column("priority", String(32), nullable=False, server_default="NORMAL"),
    Column("last_validation", String(64)), Column("last_success", String(64)),
    Column("collector_source_id", ForeignKey("job_sources.id")),
    constraints=(UniqueConstraint("company_id", "provider", "tenant", "career_url"),),
)
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
posting_value_candidates = table(
    "posting_value_candidates",
    Column("posting_id", ForeignKey("job_postings.id"), nullable=False),
    Column("posting_snapshot_id", ForeignKey("posting_snapshots.id"), nullable=False),
    Column("company_source_id", ForeignKey("company_job_sources.id"), nullable=False),
    Column("quality_observation_id", ForeignKey("source_quality_observations.id"), nullable=False),
    Column("observed_at", String(64), nullable=False),
    Column("normalized", JSON, nullable=False),
    Column("application_url", String(2000), nullable=False),
)
Index("ix_posting_value_candidates_posting_time", posting_value_candidates.c.posting_id,
      posting_value_candidates.c.observed_at)
Index("ix_posting_value_candidates_quality", posting_value_candidates.c.quality_observation_id)
source_application_link_observations = table(
    "source_application_link_observations",
    Column("candidate_id", ForeignKey("posting_value_candidates.id"), nullable=False),
    Column("observed_at", String(64), nullable=False),
    Column("requested_url", String(2000), nullable=False),
    Column("final_url", String(2000)),
    Column("result", String(24), nullable=False),
    Column("reason", String(100), nullable=False),
    Column("response_status", Integer),
    Column("response_sha256", String(64)),
    Column("identity_evidence", JSON, nullable=False),
    constraints=(
        UniqueConstraint("candidate_id"),
        CheckConstraint("result IN ('ACCURATE', 'INACCURATE', 'UNKNOWN')", name="application_link_result_valid"),
    ),
)
Index("ix_source_application_link_observed_at", source_application_link_observations.c.observed_at)
company_discovery_requests = table(
    "company_discovery_requests",
    Column("company_id", ForeignKey("companies.id"), nullable=False),
    Column("company_source_id", ForeignKey("company_job_sources.id"), nullable=False),
    Column("candidate_id", ForeignKey("posting_value_candidates.id"), nullable=False),
    Column("trigger_job_id", ForeignKey("canonical_jobs.id"), nullable=False),
    Column("profile_id", ForeignKey("career_profiles.id")),
    Column("profile_version", String(64)),
    Column("reasons", JSON, nullable=False),
    Column("highest_match_score", Float),
    Column("created_at", String(64), nullable=False),
    Column("last_seen_at", String(64), nullable=False),
    Column("state", String(32), nullable=False),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("selected_website", String(2000)),
    Column("authorization", String(32)),
    Column("domain_evidence", JSON, nullable=False),
    Column("source_snapshot", JSON, nullable=False),
    Column("operation_id", ForeignKey("worker_operations.id")),
    constraints=(
        UniqueConstraint("company_id", "company_source_id"),
        CheckConstraint("state IN ('NEEDS_ORIGIN_REVIEW', 'QUEUED', 'DISMISSED')", name="discovery_request_state_valid"),
    ),
)
Index("ix_company_discovery_requests_last_seen", company_discovery_requests.c.last_seen_at)
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
source_quality_observations = table("source_quality_observations",
    Column("configured_source_id", ForeignKey("job_sources.id")),
    Column("company_source_id", ForeignKey("company_job_sources.id")),
    Column("source_endpoint", String(2000)),
    Column("observed_at", String(64), nullable=False),
    Column("succeeded", Boolean, nullable=False), Column("available", Boolean),
    Column("enumerated", Integer), Column("accepted", Integer),
    Column("completeness_sum", Float), Column("salary_known", Integer),
    Column("links_checked", Integer), Column("links_accurate", Integer),
    Column("error_code", String(100)),
    constraints=(
        CheckConstraint("(configured_source_id IS NOT NULL AND company_source_id IS NULL) OR "
                        "(configured_source_id IS NULL AND company_source_id IS NOT NULL)", name="quality_one_source"),
        CheckConstraint("enumerated IS NULL OR enumerated >= 0", name="quality_enumerated_nonnegative"),
        CheckConstraint("accepted IS NULL OR (enumerated IS NOT NULL AND accepted >= 0 AND accepted <= enumerated)", name="quality_accepted_bounds"),
        CheckConstraint("completeness_sum IS NULL OR (accepted IS NOT NULL AND completeness_sum >= 0 AND completeness_sum <= accepted)", name="quality_completeness_bounds"),
        CheckConstraint("salary_known IS NULL OR (accepted IS NOT NULL AND salary_known >= 0 AND salary_known <= accepted)", name="quality_salary_bounds"),
        CheckConstraint("links_checked IS NULL OR (accepted IS NOT NULL AND links_checked >= 0 AND links_checked <= accepted)", name="quality_links_bounds"),
        CheckConstraint("links_accurate IS NULL OR (links_checked IS NOT NULL AND links_accurate >= 0 AND links_accurate <= links_checked)", name="quality_accuracy_bounds"),
    ))
Index("ix_quality_configured_time", source_quality_observations.c.configured_source_id, source_quality_observations.c.observed_at)
Index("ix_quality_company_time", source_quality_observations.c.company_source_id, source_quality_observations.c.observed_at)
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
