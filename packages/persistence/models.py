"""Relational authority for the initial product spine."""
from sqlalchemy import Boolean, CheckConstraint, Column, Float, ForeignKey, Index, Integer, JSON, LargeBinary, MetaData, String, Table, Text, UniqueConstraint, false, true

metadata = MetaData()


def table(name, *columns, constraints=()):
    return Table(name, metadata, Column("id", String(128), primary_key=True), *columns, *constraints)


workspace_identity = table("workspace_identity", Column("operation_mode", String(16), nullable=False),
                           Column("bound_at", String(64), nullable=False))


profiles = table("career_profiles", Column("name", String(200), nullable=False),
                 Column("email", String(320), nullable=False), Column("version", String(64), nullable=False))
profile_versions = table("profile_versions", Column("profile_id", ForeignKey("career_profiles.id"), nullable=False),
                         Column("version", String(64), nullable=False), Column("contact", JSON, nullable=False), Column("answers", JSON, nullable=False))
ai_task_settings = table(
    "profile_ai_task_settings",
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("task", String(64), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("model", String(128)),
    Column("updated_at", String(64), nullable=False),
    constraints=(UniqueConstraint("profile_id", "task",
                                  name="uq_profile_ai_task_settings_profile_task"),
                 CheckConstraint("(provider = 'disabled' AND model IS NULL) OR "
                                 "(provider <> 'disabled' AND model IS NOT NULL)",
                                 name="ck_profile_ai_task_provider_model")),
)
provider_credentials = table(
    "provider_credentials",
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("encrypted_secret", LargeBinary, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    Column("last_tested_at", String(64)),
    Column("last_test_status", String(24), nullable=False, server_default="NOT_TESTED"),
    Column("rotation_count", Integer, nullable=False, server_default="0"),
    constraints=(UniqueConstraint("profile_id", "provider", name="uq_provider_credentials_profile_provider"),
                 CheckConstraint("provider IN ('openai', 'anthropic')", name="ck_provider_credentials_provider"),
                 CheckConstraint("last_test_status IN ('NOT_TESTED', 'CONNECTED', 'FAILED')",
                                 name="ck_provider_credentials_test_status"),
                 CheckConstraint("rotation_count >= 0", name="ck_provider_credentials_rotation_count")),
)
ai_cost_policies = table(
    "profile_ai_cost_policies",
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("minimum_score", Integer, nullable=False),
    Column("max_input_candidates", Integer, nullable=False),
    Column("max_ai_candidates", Integer, nullable=False),
    Column("max_document_candidates", Integer, nullable=False),
    Column("updated_at", String(64), nullable=False),
    constraints=(UniqueConstraint("profile_id", name="uq_profile_ai_cost_policy_profile"),
                 CheckConstraint("minimum_score >= 0 AND minimum_score <= 100 AND "
                                 "max_input_candidates >= 1 AND max_input_candidates <= 100000 AND "
                                 "max_ai_candidates >= 0 AND max_ai_candidates <= 75 AND "
                                 "max_document_candidates >= 0 AND max_document_candidates <= 20 AND "
                                 "max_document_candidates <= max_ai_candidates",
                                 name="ck_profile_ai_cost_policy_bounds")),
)
answer_confidence_policies = table(
    "profile_answer_confidence_policies",
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("minimum_saved_answer_confidence", Integer, nullable=False),
    Column("minimum_question_match_confidence", Float, nullable=False),
    Column("updated_at", String(64), nullable=False),
    constraints=(UniqueConstraint("profile_id", name="uq_profile_answer_confidence_policy_profile"),
                 CheckConstraint(
                     "minimum_saved_answer_confidence >= 80 AND minimum_saved_answer_confidence <= 100 AND "
                     "minimum_question_match_confidence >= 0.75 AND minimum_question_match_confidence <= 1.0",
                     name="ck_profile_answer_confidence_policy_bounds")),
)
ai_evaluation_runs = table(
    "ai_evaluation_runs",
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("profile_version", String(64), nullable=False),
    Column("policy", JSON, nullable=False),
    Column("task_provider", String(64), nullable=False),
    Column("task_model", String(128), nullable=False),
    Column("evaluated_candidate_count", Integer, nullable=False),
    Column("created_at", String(64), nullable=False),
    constraints=(CheckConstraint("evaluated_candidate_count >= 0",
                                 name="ck_ai_evaluation_runs_candidate_count"),),
)
Index("ix_ai_evaluation_runs_profile_created", ai_evaluation_runs.c.profile_id,
      ai_evaluation_runs.c.created_at)
ai_job_evaluations = table(
    "ai_job_evaluations",
    Column("run_id", ForeignKey("ai_evaluation_runs.id", ondelete="CASCADE"), nullable=False),
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", String(128), nullable=False),
    Column("title", String(512), nullable=False),
    Column("company", String(512), nullable=False),
    Column("deterministic_score", Integer, nullable=False),
    Column("category", String(16), nullable=False),
    Column("rationale", String(1000), nullable=False),
    Column("created_at", String(64), nullable=False),
    constraints=(
        UniqueConstraint("run_id", "job_id", name="uq_ai_job_evaluation_run_job"),
        CheckConstraint("deterministic_score >= 0 AND deterministic_score <= 100",
                        name="ck_ai_job_evaluation_score"),
        CheckConstraint("category IN ('STRONG', 'REVIEW', 'WEAK')",
                        name="ck_ai_job_evaluation_category"),
    ),
)
Index("ix_ai_job_evaluations_profile_created", ai_job_evaluations.c.profile_id,
      ai_job_evaluations.c.created_at)
ai_document_reviews = table(
    "ai_document_reviews",
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("run_id", ForeignKey("ai_evaluation_runs.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", String(128), nullable=False),
    Column("reviewed_at", String(64), nullable=False),
    constraints=(UniqueConstraint("run_id", "job_id", name="uq_ai_document_review_run_job"),),
)
facts = table("career_facts", Column("profile_id", ForeignKey("career_profiles.id"), nullable=False),
              Column("version", String(64), nullable=False), Column("fact_id", String(128), nullable=False),
              Column("kind", String(32), nullable=False), Column("text", String(10000), nullable=False),
              Column("source", String(1000), nullable=False), Column("verified", Boolean, nullable=False),
              Column("user_approved", Boolean, nullable=False), Column("confidence", Float, nullable=False))
career_stories = table(
    "career_stories",
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("story_key", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("profile_version", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("title", String(200), nullable=False),
    Column("text", Text, nullable=False),
    Column("fact_ids", JSON, nullable=False),
    Column("fact_fingerprints", JSON, nullable=False),
    Column("tags", JSON, nullable=False),
    Column("allowed_uses", JSON, nullable=False),
    Column("review_status", String(16), nullable=False),
    Column("review_note", String(2000)),
    Column("created_at", String(64), nullable=False),
    Column("reviewed_at", String(64)),
    constraints=(
        UniqueConstraint("profile_id", "story_key", "revision",
                         name="uq_career_story_profile_key_revision"),
        CheckConstraint("revision >= 1", name="ck_career_story_revision_positive"),
        CheckConstraint("kind IN ('STAR_STORY', 'ACCOMPLISHMENT', 'RESUME_BULLET')",
                        name="ck_career_story_kind"),
        CheckConstraint("length(trim(title)) >= 1 AND length(title) <= 200",
                        name="ck_career_story_title_length"),
        CheckConstraint("length(trim(text)) >= 1 AND length(text) <= 8000",
                        name="ck_career_story_text_length"),
        CheckConstraint("review_status IN ('PENDING', 'APPROVED', 'REJECTED')",
                        name="ck_career_story_review_status"),
        CheckConstraint("review_note IS NULL OR length(review_note) <= 2000",
                        name="ck_career_story_review_note_length"),
        CheckConstraint(
            "(review_status = 'PENDING' AND reviewed_at IS NULL) OR "
            "(review_status IN ('APPROVED', 'REJECTED') AND reviewed_at IS NOT NULL)",
            name="ck_career_story_review_timestamp"),
    ),
)
Index("ix_career_stories_profile_created", career_stories.c.profile_id,
      career_stories.c.created_at)
interview_prep_packs = table(
    "interview_prep_packs",
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", ForeignKey("canonical_jobs.id"), nullable=False),
    Column("pack_version", Integer, nullable=False),
    Column("profile_version", String(64), nullable=False),
    Column("job_fingerprint", String(64), nullable=False),
    Column("selected_story_revision_ids", JSON, nullable=False),
    Column("questions", JSON, nullable=False),
    Column("talking_points", JSON, nullable=False),
    Column("star_prompts", JSON, nullable=False),
    Column("missing_evidence", JSON, nullable=False),
    Column("export_text", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    constraints=(
        UniqueConstraint("profile_id", "job_id", "pack_version",
                         name="uq_interview_prep_pack_profile_job_version"),
        CheckConstraint("pack_version >= 1", name="ck_interview_prep_pack_version_positive"),
    ),
)
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
                         Column("document_kind", String(32), nullable=False, server_default="BASE_RESUME"),
                         constraints=(UniqueConstraint("profile_id", "series_id", "version"),
                                      UniqueConstraint("profile_id", "series_id", "upload_id"),
                                      CheckConstraint("document_kind IN ('BASE_RESUME','MASTER_RESUME','TAILORED_RESUME','COVER_LETTER','APPLICATION_ANSWER_EXPORT','SUPPORTING_DOCUMENT','CERTIFICATION')", name="ck_resume_revision_kind")))
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
company_contexts = table(
    "company_contexts",
    Column("company_id", ForeignKey("companies.id"), nullable=False),
    Column("company_source_id", ForeignKey("company_job_sources.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("request_id", String(64), nullable=False),
    Column("content", Text, nullable=False),
    Column("content_origin", String(32), nullable=False, server_default="USER_PROVIDED"),
    Column("content_status", String(16), nullable=False, server_default="UNVERIFIED"),
    Column("content_sha256", String(64), nullable=False),
    Column("source_url", String(2000), nullable=False),
    Column("source_host", String(253), nullable=False),
    Column("verified_domain", String(253)),
    Column("source_identity_method", String(64)),
    Column("identity_status", String(16), nullable=False),
    Column("user_reviewed", Boolean, nullable=False, server_default=false()),
    Column("user_approved", Boolean, nullable=False, server_default=false()),
    Column("reviewed_at", String(64)),
    Column("created_at", String(64), nullable=False),
    constraints=(UniqueConstraint("company_source_id", "version",
                                  name="uq_company_context_source_version"),
                 UniqueConstraint("company_source_id", "request_id",
                                  name="uq_company_context_source_request"),
                 CheckConstraint("content_origin = 'USER_PROVIDED'",
                                 name="ck_company_context_origin"),
                 CheckConstraint("content_status = 'UNVERIFIED'",
                                 name="ck_company_context_content_status"),
                 CheckConstraint("identity_status IN ('VERIFIED','UNVERIFIED')",
                                 name="ck_company_context_identity_status"),
                 CheckConstraint("user_approved = false OR (user_reviewed = true AND identity_status = 'VERIFIED')",
                                 name="ck_company_context_approval_gate")),
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
job_lifecycle_observations = table(
    "job_lifecycle_observations",
    Column("job_id", ForeignKey("canonical_jobs.id"), nullable=False),
    Column("posting_id", ForeignKey("job_postings.id"), nullable=False),
    Column("candidate_id", ForeignKey("posting_value_candidates.id")),
    Column("quality_observation_id", ForeignKey("source_quality_observations.id")),
    Column("observed_at", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("authority", String(32), nullable=False),
    Column("evidence", JSON, nullable=False),
    constraints=(CheckConstraint("status IN ('ACTIVE','POSSIBLY_ACTIVE','REMOVED','EXPIRED','FILLED','UNKNOWN','CLOSED')", name="job_lifecycle_status_valid"),),
)
Index("ix_lifecycle_job_time", job_lifecycle_observations.c.job_id, job_lifecycle_observations.c.observed_at)
job_description_revisions = table(
    "job_description_revisions",
    Column("job_id", ForeignKey("canonical_jobs.id"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("candidate_id", ForeignKey("posting_value_candidates.id")),
    Column("description_hash", String(64), nullable=False),
    Column("material_hash", String(64), nullable=False),
    Column("description", String(50000), nullable=False),
    Column("salary", JSON, nullable=False),
    Column("location", String(1000)),
    Column("remote_policy", JSON, nullable=False),
    Column("observed_at", String(64)),
    Column("recorded_at", String(64), nullable=False),
    constraints=(UniqueConstraint("job_id", "sequence"),),
)
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
source_candidates = table(
    "source_candidates",
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("domain", String(253), nullable=False),
    Column("display_name", String(253), nullable=False),
    Column("site_type", String(32), nullable=False, server_default="UNKNOWN"),
    Column("state", String(32), nullable=False, server_default="NEEDS_REVIEW"),
    Column("first_seen_at", String(64), nullable=False),
    Column("last_seen_at", String(64), nullable=False),
    Column("collection_enabled", Boolean, nullable=False, server_default=false()),
    Column("review_reason", String(1000)),
    Column("integration_issue", Integer),
    constraints=(
        UniqueConstraint("profile_id", "domain", name="uq_source_candidate_profile_domain"),
        CheckConstraint("site_type IN ('JOB_BOARD', 'EMPLOYER', 'ATS', 'UNKNOWN')",
                        name="ck_source_candidate_site_type"),
        CheckConstraint("state IN ('DISCOVERED', 'NEEDS_REVIEW', 'INTEGRATION_QUEUED', 'INTEGRATED', 'BLOCKED', 'REJECTED')",
                        name="ck_source_candidate_state"),
    ),
)
Index("ix_source_candidates_profile_state", source_candidates.c.profile_id,
      source_candidates.c.state, source_candidates.c.last_seen_at)
source_candidate_observations = table(
    "source_candidate_observations",
    Column("candidate_id", ForeignKey("source_candidates.id", ondelete="CASCADE"), nullable=False),
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("capture_key", String(128), nullable=False),
    Column("job_id", ForeignKey("canonical_jobs.id", ondelete="SET NULL")),
    Column("application_id", ForeignKey("applications.id", ondelete="SET NULL")),
    Column("discovery_origin", String(253)),
    Column("origin_attribution", String(32), nullable=False),
    Column("source_url", String(2000)),
    Column("application_url", String(2000)),
    Column("observed_hops", JSON, nullable=False),
    Column("capture_method", String(32), nullable=False),
    Column("evidence_status", String(32), nullable=False),
    Column("observed_at", String(64), nullable=False),
    constraints=(
        UniqueConstraint("profile_id", "capture_key", name="uq_source_candidate_observation_profile_capture"),
        CheckConstraint("origin_attribution IN ('USER_REPORTED', 'UNKNOWN')",
                        name="ck_source_candidate_origin_attribution"),
        CheckConstraint("capture_method IN ('MANUAL_CAPTURE', 'EXTERNAL_APPLICATION', 'SUPPORTED_COLLECTION')",
                        name="ck_source_candidate_capture_method"),
        CheckConstraint("evidence_status IN ('USER_REPORTED', 'PARTIAL', 'UNKNOWN')",
                        name="ck_source_candidate_evidence_status"),
    ),
)
Index("ix_source_candidate_observations_candidate_time",
      source_candidate_observations.c.candidate_id,
      source_candidate_observations.c.observed_at)
source_candidate_reviews = table(
    "source_candidate_reviews",
    Column("candidate_id", ForeignKey("source_candidates.id", ondelete="CASCADE"), nullable=False),
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("state", String(32), nullable=False),
    Column("site_type", String(32), nullable=False, server_default="UNKNOWN"),
    Column("reason", String(1000), nullable=False),
    Column("api_feed_evidence_status", String(32), nullable=False),
    Column("api_feed_evidence_reference", String(2000)),
    Column("api_feed_evidence_note", String(1000), nullable=False),
    Column("access_terms_note", String(1000), nullable=False),
    Column("capabilities_limits_note", String(1000), nullable=False),
    Column("expected_value_maintenance_note", String(1000), nullable=False),
    Column("integration_issue", Integer),
    Column("reviewed_at", String(64), nullable=False),
    constraints=(CheckConstraint("state IN ('NEEDS_REVIEW', 'INTEGRATION_QUEUED', 'INTEGRATED', 'BLOCKED', 'REJECTED')",
                                 name="ck_source_candidate_review_state"),
                 CheckConstraint("site_type IN ('JOB_BOARD', 'EMPLOYER', 'ATS', 'UNKNOWN')",
                                 name="ck_source_candidate_review_site_type"),
                 CheckConstraint("api_feed_evidence_status IN ('PUBLIC_DOCUMENTED', 'UNKNOWN', 'BLOCKED')",
                                 name="ck_source_candidate_review_evidence_status")),
)
Index("ix_source_candidate_reviews_candidate_time", source_candidate_reviews.c.candidate_id,
      source_candidate_reviews.c.reviewed_at)
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
company_source_monitoring = table(
    "company_source_monitoring",
    Column("source_id", ForeignKey("company_job_sources.id"), nullable=False),
    Column("source_snapshot", JSON, nullable=False),
    Column("authorization", String(32), nullable=False),
    Column("domain_evidence", JSON, nullable=False),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("enabled", Boolean, nullable=False, server_default=true()),
    Column("poll_interval", Integer, nullable=False, server_default="3600"),
    Column("status", String(32), nullable=False, server_default="IDLE"),
    Column("last_attempt", String(64)),
    Column("next_due", String(64)),
    Column("last_error", String(128)),
    Column("failure_count", Integer, nullable=False, server_default="0"),
    Column("operation_id", ForeignKey("worker_operations.id")),
    Column("rediscovery_operation_id", ForeignKey("worker_operations.id")),
    Column("completed_operation_id", ForeignKey("worker_operations.id")),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    constraints=(
        UniqueConstraint("source_id"),
        CheckConstraint('"authorization" IN (\'VERIFIED_DOMAIN\', \'USER_REVIEWED\')', name="company_source_monitoring_authorization_valid"),
        CheckConstraint("poll_interval BETWEEN 300 AND 604800", name="company_source_monitoring_poll_interval_valid"),
        CheckConstraint("status IN ('IDLE', 'SCHEDULED', 'HEALTHY', 'FAILED', 'BLOCKED', 'REDISCOVERY_REQUIRED')", name="company_source_monitoring_status_valid"),
    ),
)
Index("ix_company_source_monitoring_next_due", company_source_monitoring.c.next_due)
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
                  Column("sha256", JSON, nullable=False), Column("template", String(128), nullable=False),
                  Column("created_at", String(64)), Column("generation_model", String(128)),
                  Column("generation_context", JSON))
attempts = table("application_attempts", Column("application_id", ForeignKey("applications.id"), nullable=False),
                 Column("state", String(64), nullable=False), Column("steps", JSON, nullable=False), Column("started_at", String(64), nullable=False))
operations = table("worker_operations", Column("kind", String(64), nullable=False), Column("entity_id", String(128), nullable=False),
                   Column("params", JSON, nullable=False), Column("state", String(64), nullable=False),
                   Column("error", String(1000)), Column("updated_at", String(64), nullable=False),
                   Column("attempt_count", Integer, nullable=False, server_default="0"),
                   Column("current_step", String(128)),
                   Column("progress", Integer, nullable=False, server_default="0"),
                   Column("started_at", String(64)), Column("completed_at", String(64)),
                   Column("retry_count", Integer, nullable=False, server_default="0"),
                   Column("recovery_metadata", JSON, nullable=False, server_default="{}"),
                   Column("cancel_requested", Boolean, nullable=False, server_default=false()),
                   Column("lease_expires_at", String(64)),
                   Column("last_attempt_at", String(64)), Column("dead_lettered_at", String(64)),
                   Column("next_attempt_at", String(64)),
                   Column("discarded_at", String(64)),
                   constraints=(CheckConstraint(
                       "progress >= 0 AND progress <= 100",
                       name="ck_worker_operations_progress_range",
                   ),))

Index("ix_worker_operations_retry_due", operations.c.state, operations.c.next_attempt_at)
uploaded_comparison_results = Table(
    "uploaded_comparison_results", metadata,
    Column("id", String(128), ForeignKey("worker_operations.id", ondelete="CASCADE"), primary_key=True),
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("application_id", ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
    Column("result", JSON, nullable=False),
    Column("created_at", String(64), nullable=False),
)
resume_extraction_results = Table(
    "resume_extraction_results", metadata,
    Column("id", String(128), ForeignKey("worker_operations.id", ondelete="CASCADE"), primary_key=True),
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("resume_id", ForeignKey("uploaded_resumes.id", ondelete="CASCADE"), nullable=False),
    Column("resume_sha256", String(64), nullable=False),
    Column("extractor_version", Integer, nullable=False),
    Column("result", JSON, nullable=False),
    Column("created_at", String(64), nullable=False),
)
audit = table("audit_events", Column("entity_id", String(128), nullable=False), Column("action", String(64), nullable=False),
              Column("payload", JSON, nullable=False), Column("timestamp", String(64), nullable=False))
