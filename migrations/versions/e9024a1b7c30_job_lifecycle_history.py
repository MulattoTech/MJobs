"""Persist job lifecycle and description history without fabricating legacy rows."""
from alembic import op
import sqlalchemy as sa


revision = "e9024a1b7c30"
down_revision = "d8f0c1b2a3e4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "job_lifecycle_observations",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("job_id", sa.String(length=128), sa.ForeignKey("canonical_jobs.id"), nullable=False),
        sa.Column("posting_id", sa.String(length=128), sa.ForeignKey("job_postings.id"), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), sa.ForeignKey("posting_value_candidates.id")),
        sa.Column("quality_observation_id", sa.String(length=128), sa.ForeignKey("source_quality_observations.id")),
        sa.Column("observed_at", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE','POSSIBLY_ACTIVE','REMOVED','EXPIRED','FILLED','UNKNOWN','CLOSED')",
            name="job_lifecycle_status_valid",
        ),
    )
    op.create_index(
        "ix_lifecycle_job_time",
        "job_lifecycle_observations",
        ["job_id", "observed_at"],
    )
    op.create_table(
        "job_description_revisions",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("job_id", sa.String(length=128), sa.ForeignKey("canonical_jobs.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), sa.ForeignKey("posting_value_candidates.id")),
        sa.Column("description_hash", sa.String(length=64), nullable=False),
        sa.Column("material_hash", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=50000), nullable=False),
        sa.Column("salary", sa.JSON(), nullable=False),
        sa.Column("location", sa.String(length=1000)),
        sa.Column("remote_policy", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.String(length=64)),
        sa.Column("recorded_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("job_id", "sequence"),
    )


def downgrade():
    connection = op.get_bind()
    lifecycle_populated = connection.execute(
        sa.text("SELECT 1 FROM job_lifecycle_observations LIMIT 1")
    ).first()
    description_populated = connection.execute(
        sa.text("SELECT 1 FROM job_description_revisions LIMIT 1")
    ).first()
    if lifecycle_populated:
        raise RuntimeError(
            "Refusing to discard job lifecycle history; restore a verified backup for intentional rollback"
        )
    if description_populated:
        raise RuntimeError(
            "Refusing to discard job description history; restore a verified backup for intentional rollback"
        )
    op.drop_table("job_description_revisions")
    op.drop_index("ix_lifecycle_job_time", table_name="job_lifecycle_observations")
    op.drop_table("job_lifecycle_observations")
