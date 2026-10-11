"""Persist queued uploaded comparison results without rewriting inline history."""
from alembic import op
import sqlalchemy as sa

revision = '20261011uc01'
down_revision = '20261011rx01'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('uploaded_comparison_results',
        sa.Column('id', sa.String(128), sa.ForeignKey('worker_operations.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('profile_id', sa.String(128), sa.ForeignKey('career_profiles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('application_id', sa.String(128), sa.ForeignKey('applications.id', ondelete='CASCADE'), nullable=False),
        sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.String(64), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM uploaded_comparison_results LIMIT 1')).first():
        raise RuntimeError('Refusing to discard durable uploaded comparison results')
    op.drop_table('uploaded_comparison_results')
