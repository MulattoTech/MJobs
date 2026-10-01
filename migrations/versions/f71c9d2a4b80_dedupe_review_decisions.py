"""Add persistent duplicate-review decisions.

Revision ID: f71c9d2a4b80
Revises: d5a9c38e21f4
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f71c9d2a4b80"
down_revision: Union[str, Sequence[str], None] = "d5a9c38e21f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dedupe_reviews",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("left_job_id", sa.String(length=128), sa.ForeignKey("canonical_jobs.id"), nullable=False),
        sa.Column("right_job_id", sa.String(length=128), sa.ForeignKey("canonical_jobs.id"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("left_job_id", "right_job_id"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM dedupe_reviews LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard duplicate-review decisions; restore a verified backup for intentional rollback"
        )
    op.drop_table("dedupe_reviews")
