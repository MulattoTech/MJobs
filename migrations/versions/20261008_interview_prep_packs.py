"""Persist immutable, profile-scoped interview-preparation pack versions.

Revision ID: 20261008ip01
Revises: c4f08e16a2d9
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261008ip01"
down_revision: Union[str, Sequence[str], None] = "c4f08e16a2d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "interview_prep_packs",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column(
            "profile_id",
            sa.String(length=128),
            sa.ForeignKey("career_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.String(length=128),
            sa.ForeignKey("canonical_jobs.id"),
            nullable=False,
        ),
        sa.Column("pack_version", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.String(length=64), nullable=False),
        sa.Column("job_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("selected_story_revision_ids", sa.JSON(), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("talking_points", sa.JSON(), nullable=False),
        sa.Column("star_prompts", sa.JSON(), nullable=False),
        sa.Column("missing_evidence", sa.JSON(), nullable=False),
        sa.Column("export_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint(
            "profile_id", "job_id", "pack_version",
            name="uq_interview_prep_pack_profile_job_version",
        ),
        sa.CheckConstraint(
            "pack_version >= 1",
            name="ck_interview_prep_pack_version_positive",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM interview_prep_packs LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard interview-preparation packs; restore a verified backup "
            "for intentional rollback"
        )
    op.drop_table("interview_prep_packs")
