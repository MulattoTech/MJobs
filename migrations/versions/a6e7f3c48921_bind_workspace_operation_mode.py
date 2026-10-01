"""Bind each database to one operation mode without rewriting existing data.

Revision ID: a6e7f3c48921
Revises: 792dc0dc5664
Create Date: 2026-09-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6e7f3c48921"
down_revision: Union[str, Sequence[str], None] = "792dc0dc5664"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("workspace_identity",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("operation_mode", sa.String(length=16), nullable=False),
        sa.Column("bound_at", sa.String(length=64), nullable=False))


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM workspace_identity LIMIT 1")).first():
        raise RuntimeError("Refusing to drop a bound workspace identity; restore a verified backup for an intentional rollback")
    op.drop_table("workspace_identity")
