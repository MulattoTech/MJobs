"""Preserve target snapshot and explicit skill-ordering decisions."""
from alembic import op
import sqlalchemy as sa

revision = 'cf4216a93b85'
down_revision = 'be3105f82a74'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('documents',sa.Column('generation_context',sa.JSON()))


def downgrade():
    rows=op.get_bind().execute(sa.text('SELECT generation_context FROM documents')).all()
    if any(row[0] is not None and row[0] != 'null' for row in rows):
        raise RuntimeError('Refusing to discard document skill-order context; restore a verified backup for intentional rollback')
    with op.batch_alter_table('documents') as batch:
        batch.drop_column('generation_context')
