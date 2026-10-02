"""Persist whether a company source is automatic or user watched.

Revision ID: b71d9a42c0e3
Revises: e14ac893620f
Create Date: 2026-10-02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b71d9a42c0e3"
down_revision: Union[str, Sequence[str], None] = "e14ac893620f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("company_job_sources") as batch:
        batch.add_column(sa.Column("origin", sa.String(length=32), nullable=False,
                                   server_default="AUTO_DISCOVERED"))
    connection = op.get_bind()
    connection.execute(sa.text(
        "UPDATE company_job_sources SET origin = CASE "
        "WHEN discovered_automatically THEN 'AUTO_DISCOVERED' ELSE 'USER_WATCHED' END"
    ))


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM company_job_sources "
            "WHERE origin = 'USER_WATCHED' OR priority = 'USER_PINNED' LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard company source watch or pin decisions; "
            "restore a verified backup for intentional rollback"
        )
    with op.batch_alter_table("company_job_sources") as batch:
        batch.drop_column("origin")
