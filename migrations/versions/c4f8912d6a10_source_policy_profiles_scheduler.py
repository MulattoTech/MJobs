"""Persist source capabilities/policy and data-driven search profiles.

Revision ID: c4f8912d6a10
Revises: b7e10a34c921
Create Date: 2026-10-01
"""
from typing import Sequence, Union
import json

from alembic import op
import sqlalchemy as sa


revision: str = "c4f8912d6a10"
down_revision: Union[str, Sequence[str], None] = "b7e10a34c921"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


GREENHOUSE_CAPABILITIES = {
    "strategy": "API",
    "capabilities": [
        "APPLICATION_REDIRECT", "DISCOVERY", "JOB_DETAIL",
        "REMOTE_METADATA", "SALARY",
    ],
    "discovery": True,
    "job_detail": True,
    "salary": True,
    "remote_metadata": True,
    "application_redirect": True,
    "direct_application": False,
    "application_status": False,
}


def upgrade() -> None:
    op.add_column("job_sources", sa.Column(
        "capabilities", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("job_sources", sa.Column(
        "concurrency", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("job_sources", sa.Column(
        "request_timeout", sa.Integer(), nullable=False, server_default="10"))
    op.add_column("job_sources", sa.Column(
        "rate_limit", sa.Integer(), nullable=False, server_default="60"))
    op.add_column("job_sources", sa.Column(
        "jitter_seconds", sa.Integer(), nullable=False, server_default="30"))
    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE job_sources SET capabilities = :capabilities WHERE provider = 'greenhouse'"),
        {"capabilities": json.dumps(GREENHOUSE_CAPABILITIES)},
    )
    op.create_table(
        "search_profiles",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("profile_id", "name"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text(
            "SELECT 1 FROM search_profiles LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to drop search profiles; restore a verified backup for intentional rollback")
    if connection.execute(sa.text(
            "SELECT 1 FROM job_sources LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard persisted source policy; restore a verified backup for intentional rollback")
    op.drop_table("search_profiles")
    op.drop_column("job_sources", "jitter_seconds")
    op.drop_column("job_sources", "rate_limit")
    op.drop_column("job_sources", "request_timeout")
    op.drop_column("job_sources", "concurrency")
    op.drop_column("job_sources", "capabilities")
