"""Add explicit source/application URLs to raw job postings.

Revision ID: d5a9c38e21f4
Revises: c4f8912d6a10
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5a9c38e21f4"
down_revision: Union[str, Sequence[str], None] = "c4f8912d6a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("source_url", sa.String(length=2000), nullable=True))
    op.add_column("job_postings", sa.Column("application_url", sa.String(length=2000), nullable=True))
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE job_postings SET source_url = source WHERE source_url IS NULL"))
    connection.execute(sa.text(
        "UPDATE job_postings SET application_url = "
        "(SELECT application_routes.url FROM application_routes "
        "WHERE application_routes.job_id = job_postings.job_id "
        "ORDER BY application_routes.id LIMIT 1) "
        "WHERE application_url IS NULL"
    ))
    with op.batch_alter_table("job_postings") as batch:
        batch.alter_column("source_url", existing_type=sa.String(length=2000), nullable=False)
        batch.alter_column("application_url", existing_type=sa.String(length=2000), nullable=False)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM job_postings LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard explicit posting provenance; restore a verified backup for intentional rollback")
    op.drop_column("job_postings", "application_url")
    op.drop_column("job_postings", "source_url")
