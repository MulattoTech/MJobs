"""Add evidence-backed indexes for core database query paths.

Revision ID: 20261025di01
Revises: 20261024dc01
Create Date: 2026-10-25
"""

from alembic import op
import sqlalchemy as sa


revision = "20261025di01"
down_revision = "20261024dc01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_canonical_jobs_company_id_id",
        "canonical_jobs",
        ["company_id", "id"],
    )
    op.create_index(
        "ix_job_matches_job_profile_version",
        "job_matches",
        ["job_id", "profile_id", "profile_version"],
    )
    op.create_index(
        "ix_applications_profile_created_id",
        "applications",
        ["profile_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_company_job_sources_enabled_id",
        "company_job_sources",
        ["id"],
        sqlite_where=sa.text("enabled IS 1"),
        postgresql_where=sa.text("enabled IS TRUE"),
    )


def downgrade():
    op.drop_index("ix_company_job_sources_enabled_id", table_name="company_job_sources")
    op.drop_index("ix_applications_profile_created_id", table_name="applications")
    op.drop_index("ix_job_matches_job_profile_version", table_name="job_matches")
    op.drop_index("ix_canonical_jobs_company_id_id", table_name="canonical_jobs")
