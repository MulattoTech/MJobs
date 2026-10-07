"""Add PostgreSQL full-text indexes for canonical job search."""
from typing import Sequence, Union

from alembic import op


revision: str = "f37a0c2e91b5"
down_revision: Union[str, Sequence[str], None] = "d5a7c81e240b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE INDEX ix_canonical_jobs_full_text
        ON canonical_jobs USING GIN (
            to_tsvector(
                'simple'::regconfig,
                coalesce(title, '') || ' ' ||
                coalesce(description, '') || ' ' ||
                coalesce(skills::text, '') || ' ' ||
                coalesce(location, '')
            )
        )
    """)
    op.execute("""
        CREATE INDEX ix_companies_name_full_text
        ON companies USING GIN (
            to_tsvector('simple'::regconfig, coalesce(name, ''))
        )
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_companies_name_full_text")
    op.execute("DROP INDEX IF EXISTS ix_canonical_jobs_full_text")
