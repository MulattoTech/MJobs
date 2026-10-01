"""Add planned application route choices without deleting source records.

Revision ID: a82e4c19d753
Revises: f71c9d2a4b80
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a82e4c19d753"
down_revision: Union[str, Sequence[str], None] = "f71c9d2a4b80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "application_route_choices",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("application_id", sa.String(length=128), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("route_id", sa.String(length=128), sa.ForeignKey("application_routes.id"), nullable=False),
        sa.Column("underlying_key", sa.String(length=2200), nullable=False),
        sa.Column("requested_mode", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("application_id", "underlying_key"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM application_route_choices LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard application route choices; restore a verified backup for intentional rollback"
        )
    op.drop_table("application_route_choices")
