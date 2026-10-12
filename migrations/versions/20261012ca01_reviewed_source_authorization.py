"""Retain request-scoped employer identity authorization for recurring sources."""
from alembic import op
import sqlalchemy as sa

revision = '20261012ca01'
down_revision = '20261012ai01'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('company_job_sources', sa.Column('identity_authorization', sa.JSON(), nullable=True))


def downgrade():
    row = op.get_bind().execute(sa.text("SELECT 1 FROM company_job_sources "
        "WHERE identity_authorization IS NOT NULL AND CAST(identity_authorization AS TEXT) <> 'null' LIMIT 1")).first()
    if row:
        raise RuntimeError('Refusing to discard recurring employer identity authorization')
    op.drop_column('company_job_sources', 'identity_authorization')
