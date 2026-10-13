"""Preserve the job-input identity behind advisory evaluations and reviews."""
from alembic import op
import sqlalchemy as sa

revision = '20261013ai01'
down_revision = '20261012ca01'
branch_labels = None
depends_on = None


def upgrade():
    # Existing results remain readable, with UNKNOWN freshness. Never pretend
    # that today's job content was the input to a historical provider call.
    op.add_column('ai_job_evaluations', sa.Column('job_fingerprint', sa.String(64), nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text(
            'SELECT 1 FROM ai_job_evaluations WHERE job_fingerprint IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('Refusing to discard AI evaluation input provenance')
    op.drop_column('ai_job_evaluations', 'job_fingerprint')
