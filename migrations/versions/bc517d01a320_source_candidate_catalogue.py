"""Catalogue user-supplied third-party job source candidates."""
from alembic import op
import sqlalchemy as sa


revision = "bc517d01a320"
down_revision = "f0169e7d4ac2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_candidates",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("display_name", sa.String(length=253), nullable=False),
        sa.Column("site_type", sa.String(length=32), server_default="UNKNOWN", nullable=False),
        sa.Column("state", sa.String(length=32), server_default="NEEDS_REVIEW", nullable=False),
        sa.Column("first_seen_at", sa.String(length=64), nullable=False),
        sa.Column("last_seen_at", sa.String(length=64), nullable=False),
        sa.Column("collection_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("review_reason", sa.String(length=1000)),
        sa.Column("integration_issue", sa.Integer()),
        sa.UniqueConstraint("profile_id", "domain", name="uq_source_candidate_profile_domain"),
        sa.CheckConstraint("site_type IN ('JOB_BOARD', 'EMPLOYER', 'ATS', 'UNKNOWN')",
                           name="ck_source_candidate_site_type"),
        sa.CheckConstraint("state IN ('DISCOVERED', 'NEEDS_REVIEW', 'INTEGRATION_QUEUED', 'INTEGRATED', 'BLOCKED', 'REJECTED')",
                           name="ck_source_candidate_state"),
    )
    op.create_index("ix_source_candidates_profile_state", "source_candidates",
                    ["profile_id", "state", "last_seen_at"])
    op.create_table(
        "source_candidate_observations",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("candidate_id", sa.String(length=128),
                  sa.ForeignKey("source_candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capture_key", sa.String(length=128), nullable=False),
        sa.Column("job_id", sa.String(length=128),
                  sa.ForeignKey("canonical_jobs.id", ondelete="SET NULL")),
        sa.Column("application_id", sa.String(length=128),
                  sa.ForeignKey("applications.id", ondelete="SET NULL")),
        sa.Column("discovery_origin", sa.String(length=253)),
        sa.Column("origin_attribution", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=2000)),
        sa.Column("application_url", sa.String(length=2000)),
        sa.Column("observed_hops", sa.JSON(), nullable=False),
        sa.Column("capture_method", sa.String(length=32), nullable=False),
        sa.Column("evidence_status", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("profile_id", "capture_key",
                           name="uq_source_candidate_observation_profile_capture"),
        sa.CheckConstraint("origin_attribution IN ('USER_REPORTED', 'UNKNOWN')",
                           name="ck_source_candidate_origin_attribution"),
        sa.CheckConstraint("capture_method IN ('MANUAL_CAPTURE', 'EXTERNAL_APPLICATION')",
                           name="ck_source_candidate_capture_method"),
        sa.CheckConstraint("evidence_status IN ('USER_REPORTED', 'PARTIAL', 'UNKNOWN')",
                           name="ck_source_candidate_evidence_status"),
    )
    op.create_index("ix_source_candidate_observations_candidate_time",
                    "source_candidate_observations", ["candidate_id", "observed_at"])
    op.create_table(
        "source_candidate_reviews",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("candidate_id", sa.String(length=128),
                  sa.ForeignKey("source_candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("site_type", sa.String(length=32), server_default="UNKNOWN", nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("reviewed_at", sa.String(length=64), nullable=False),
        sa.CheckConstraint("state IN ('NEEDS_REVIEW', 'INTEGRATION_QUEUED', 'INTEGRATED', 'BLOCKED', 'REJECTED')",
                           name="ck_source_candidate_review_state"),
        sa.CheckConstraint("site_type IN ('JOB_BOARD', 'EMPLOYER', 'ATS', 'UNKNOWN')",
                           name="ck_source_candidate_review_site_type"),
    )
    op.create_index("ix_source_candidate_reviews_candidate_time",
                    "source_candidate_reviews", ["candidate_id", "reviewed_at"])


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM source_candidate_reviews LIMIT 1")).first():
        raise RuntimeError("Refusing to discard source-candidate review history")
    op.drop_index("ix_source_candidate_reviews_candidate_time", table_name="source_candidate_reviews")
    op.drop_table("source_candidate_reviews")
    if connection.execute(sa.text("SELECT 1 FROM source_candidate_observations LIMIT 1")).first():
        raise RuntimeError("Refusing to discard source-candidate observations")
    op.drop_index("ix_source_candidate_observations_candidate_time",
                  table_name="source_candidate_observations")
    op.drop_table("source_candidate_observations")
    if connection.execute(sa.text("SELECT 1 FROM source_candidates LIMIT 1")).first():
        raise RuntimeError("Refusing to discard source-candidate review state")
    op.drop_index("ix_source_candidates_profile_state", table_name="source_candidates")
    op.drop_table("source_candidates")
