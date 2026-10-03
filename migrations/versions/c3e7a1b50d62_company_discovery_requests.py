"""Persist durable company-discovery trigger requests."""
from alembic import op
import sqlalchemy as sa


revision = "c3e7a1b50d62"
down_revision = "b2d6f0a19c44"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "company_discovery_requests",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("company_id", sa.String(length=128), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("company_source_id", sa.String(length=128), sa.ForeignKey("company_job_sources.id"), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), sa.ForeignKey("posting_value_candidates.id"), nullable=False),
        sa.Column("trigger_job_id", sa.String(length=128), sa.ForeignKey("canonical_jobs.id"), nullable=False),
        sa.Column("profile_id", sa.String(length=128), sa.ForeignKey("career_profiles.id")),
        sa.Column("profile_version", sa.String(length=64)),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("highest_match_score", sa.Float()),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("last_seen_at", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("selected_website", sa.String(length=2000)),
        sa.Column("authorization", sa.String(length=32)),
        sa.Column("domain_evidence", sa.JSON(), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("operation_id", sa.String(length=128), sa.ForeignKey("worker_operations.id")),
        sa.UniqueConstraint("company_id", "company_source_id"),
        sa.CheckConstraint("state IN ('NEEDS_ORIGIN_REVIEW', 'QUEUED', 'DISMISSED')", name="discovery_request_state_valid"),
    )
    op.create_index("ix_company_discovery_requests_last_seen", "company_discovery_requests", ["last_seen_at"])


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM company_discovery_requests LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard company discovery requests; restore a verified backup for intentional rollback"
        )
    op.drop_index("ix_company_discovery_requests_last_seen", table_name="company_discovery_requests")
    op.drop_table("company_discovery_requests")
