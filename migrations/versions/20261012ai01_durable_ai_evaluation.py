"""Private immutable AI inputs and per-call recovery checkpoints."""
from alembic import op
import sqlalchemy as sa

revision = '20261012ai01'
down_revision = '20261011uc01'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ai_evaluation_batches',
        sa.Column('id', sa.String(128), sa.ForeignKey('worker_operations.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('profile_id', sa.String(128), sa.ForeignKey('career_profiles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('candidate_states', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.String(64), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM ai_evaluation_batches LIMIT 1')).first():
        raise RuntimeError('Refusing to discard AI evaluation recovery history')
    op.drop_table('ai_evaluation_batches')
