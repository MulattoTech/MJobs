"""Store bounded, profile-scoped recently viewed jobs independently of dispositions.

Revision ID: 20261104rv01
Revises: 20261025di01
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = "20261104rv01"
down_revision = "20261025di01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile_job_views",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("viewed_at", sa.String(length=64), nullable=False),
        sa.Column("visit_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("profile_id", "job_id", name="uq_profile_job_views_scope_job"),
        sa.CheckConstraint("visit_order >= 1", name="ck_profile_job_views_visit_order"),
    )
    op.create_index(
        "ix_profile_job_views_scope_order", "profile_job_views",
        ["profile_id", "visit_order", "id"],
    )


def downgrade():
    # A rollback must never silently destroy private visit history.
    existing = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM profile_job_views")
    ).scalar_one()
    if existing:
        raise RuntimeError("Refusing to discard recently viewed job history; export or clear it first")
    op.drop_index("ix_profile_job_views_scope_order", table_name="profile_job_views")
    op.drop_table("profile_job_views")
