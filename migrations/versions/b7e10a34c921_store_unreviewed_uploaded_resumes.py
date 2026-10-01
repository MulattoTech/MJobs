"""Store profile-scoped uploaded resume metadata without approving career facts.

Revision ID: b7e10a34c921
Revises: a6e7f3c48921
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e10a34c921"
down_revision: Union[str, Sequence[str], None] = "a6e7f3c48921"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("uploaded_resumes",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128), sa.ForeignKey("career_profiles.id"), nullable=False),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("profile_id", "sha256"))


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM uploaded_resumes LIMIT 1")).first():
        raise RuntimeError("Refusing to drop uploaded resume metadata; restore a verified backup for intentional rollback")
    op.drop_table("uploaded_resumes")
